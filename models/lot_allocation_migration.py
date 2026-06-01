# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

"""Phase 3 migration engine for per-lot allocation.

Classifies existing esfsm.job.material records into 4 buckets:
  clean       — single lot_id matches a single lot in picking history (1:1)
  multi_lot   — picking history has multiple distinct lots, unambiguous ownership
  ambiguous   — job+product has >1 material lines; cannot auto-attribute lots
  gap         — tracked product, taken_qty > 0, no lot info available

Flow:
  1. analyze()                  — classify all materials, return counts
  2. dry_run()                  — detailed per-bucket report
  3. migrate(commit=False|True) — write allocations for clean+multi_lot, gap flags for gap
  4. (ambiguous) handled via esfsm.lot.resolution.wizard
"""

import logging
import math
from collections import defaultdict

from odoo import api, models, fields, _
from odoo.exceptions import UserError
from odoo.tools import float_is_zero, float_round

_logger = logging.getLogger(__name__)


class EsfsmLotAllocationMigration(models.AbstractModel):
    _name = 'esfsm.lot.allocation.migration'
    _description = 'Phase 3 migration engine for lot allocations'

    # ──────────────────────────────────────────────
    # Classification
    # ──────────────────────────────────────────────

    @api.model
    def _classify_materials(self):
        """Return dict with material buckets + metadata.

        {
          'clean':      [{'material_id': id, 'lot_id': lot_id, 'qty': taken}],
          'multi_lot':  [{'material_id': id, 'lots': [(lot_id, qty), ...]}],
          'ambiguous':  [{'job_id': id, 'product_id': id, 'material_ids': [...]}],
          'gap':        [material_id, ...],
          'untracked':  count,
          'already_migrated': count,
        }
        """
        Material = self.env['esfsm.job.material']

        # Find all materials with taken_qty > 0
        materials = Material.search([('taken_qty', '>', 0)])

        # Detect ambiguous job+product combos only among TRACKED products
        # (untracked materials never need allocations, so duplicates don't matter)
        tracked_materials = materials.filtered(lambda m: m.product_tracking != 'none')
        combo_counts = defaultdict(list)
        for m in tracked_materials:
            combo_counts[(m.job_id.id, m.product_id.id)].append(m.id)
        ambiguous_combos = {
            combo: mids
            for combo, mids in combo_counts.items()
            if len(mids) > 1
        }
        ambiguous_material_ids = {
            mid for mids in ambiguous_combos.values() for mid in mids
        }

        result = {
            'clean': [],
            'multi_lot': [],
            'ambiguous': [],
            'gap': [],
            'untracked': 0,
            'already_migrated': 0,
            'tracked_total': 0,
        }

        # Flatten ambiguous combos for per-wizard consumption
        for (job_id, product_id), mids in ambiguous_combos.items():
            result['ambiguous'].append({
                'job_id': job_id,
                'product_id': product_id,
                'material_ids': mids,
            })

        # Batch the picking-lot lookup for every candidate (job, product) pair
        # in ONE grouped aggregation instead of a fresh 3-table-JOIN per material
        # (was N round-trips). Candidates are the tracked, not-already-migrated,
        # non-ambiguous materials — exactly the lines that reach the per-material
        # _get_picking_lot_qtys call below. See _batch_picking_lot_qtys for the
        # identical semantics ({lot_id: qty} keyed by (job_id, product_id)).
        candidate_pairs = {
            (m.job_id.id, m.product_id.id)
            for m in materials
            if m.product_tracking != 'none'
            and not m.lot_allocation_ids
            and m.id not in ambiguous_material_ids
        }
        batched_lot_qtys = self._batch_picking_lot_qtys(candidate_pairs)

        for m in materials:
            if m.product_tracking == 'none':
                result['untracked'] += 1
                continue
            result['tracked_total'] += 1
            # Already migrated — allocations exist and sum matches
            if m.lot_allocation_ids:
                result['already_migrated'] += 1
                continue
            # Ambiguous — same job+product on >1 material lines
            if m.id in ambiguous_material_ids:
                # Tracked ambiguous lines still need resolution, but counted in 'ambiguous'
                continue

            # Lots from done pickings for this job+product (pre-batched above —
            # same result the per-material _get_picking_lot_qtys would return).
            lot_qtys = batched_lot_qtys.get((m.job_id.id, m.product_id.id), {})

            if not lot_qtys:
                # No lot history → mark as historical gap
                result['gap'].append(m.id)
            elif len(lot_qtys) == 1:
                lot_id = next(iter(lot_qtys.keys()))
                qty = lot_qtys[lot_id]
                result['clean'].append({
                    'material_id': m.id,
                    'lot_id': lot_id,
                    'qty': qty,
                    'taken_qty': m.taken_qty,
                })
            else:
                # Multiple lots from picking, but job+product is unambiguous
                # (only 1 material line for this product in the job)
                result['multi_lot'].append({
                    'material_id': m.id,
                    'lots': list(lot_qtys.items()),
                    'taken_qty': m.taken_qty,
                })

        return result

    @api.model
    def _flush_before_lot_sql(self):
        """Flush the ORM caches the raw lot-history SQL below reads directly.

        The queries hit stock_picking.state (a STORED COMPUTED field), so a
        just-validated picking whose state lives only in the ORM cache must be
        flushed to its DB column first, or `WHERE sp.state='done'` misses it
        (Odoo auto-flushes before its OWN ORM reads, but not before a
        hand-written cr.execute). No-op in the production migration (historical
        pickings are already persisted); load-bearing right after
        creating/validating pickings (e.g. in tests).
        """
        self.env['stock.picking'].flush_model(['state', 'esfsm_job_id'])
        self.env['stock.move'].flush_model(['picking_id', 'product_id'])
        self.env['stock.move.line'].flush_model(['move_id', 'lot_id', 'quantity'])

    @api.model
    def _get_picking_lot_qtys(self, material):
        """Return {lot_id: qty} aggregated from done pickings on this job
        for the material's product. Keyed by (job_id, product_id) — all
        materials sharing these keys receive the same result, so callers
        in ambiguous combos must call this ONCE per combo (not per material)
        to avoid N× amplification.

        Accepts either a single record or a recordset (uses first)."""
        if hasattr(material, 'ensure_one') and len(material) > 1:
            material = material[:1]
        self._flush_before_lot_sql()
        self.env.cr.execute("""
            SELECT sml.lot_id, SUM(sml.quantity)
            FROM stock_move_line sml
            JOIN stock_move sm ON sml.move_id = sm.id
            JOIN stock_picking sp ON sm.picking_id = sp.id
            WHERE sp.esfsm_job_id = %s
              AND sm.product_id = %s
              AND sp.state = 'done'
              AND sml.lot_id IS NOT NULL
              AND sml.quantity > 0
            GROUP BY sml.lot_id
        """, (material.job_id.id, material.product_id.id))
        return {lot_id: qty for lot_id, qty in self.env.cr.fetchall()}

    @api.model
    def _batch_picking_lot_qtys(self, job_product_pairs):
        """Batched form of _get_picking_lot_qtys: aggregate done-picking lot
        quantities for MANY (job_id, product_id) pairs in ONE grouped query.

        Returns {(job_id, product_id): {lot_id: qty}}. Pairs with no matching
        picking history are simply absent from the result (callers treat a
        missing/empty entry as "no lot history" — a historical gap), so the
        per-pair value is identical to what _get_picking_lot_qtys(material)
        would return for any material sharing those keys.

        `job_product_pairs` is an iterable of (job_id, product_id) tuples.
        Returns {} immediately for an empty set (avoids an `IN ()` SQL error).
        """
        pairs = tuple({(int(j), int(p)) for j, p in job_product_pairs})
        if not pairs:
            return {}
        self._flush_before_lot_sql()
        # psycopg2 adapts a tuple-of-tuples for a composite IN, rendering
        # `(sp.esfsm_job_id, sm.product_id) IN ((j1,p1),(j2,p2),...)`.
        self.env.cr.execute("""
            SELECT sp.esfsm_job_id, sm.product_id, sml.lot_id, SUM(sml.quantity)
            FROM stock_move_line sml
            JOIN stock_move sm ON sml.move_id = sm.id
            JOIN stock_picking sp ON sm.picking_id = sp.id
            WHERE sp.state = 'done'
              AND sml.lot_id IS NOT NULL
              AND sml.quantity > 0
              AND (sp.esfsm_job_id, sm.product_id) IN %s
            GROUP BY sp.esfsm_job_id, sm.product_id, sml.lot_id
        """, (pairs,))
        result = defaultdict(dict)
        for job_id, product_id, lot_id, qty in self.env.cr.fetchall():
            result[(job_id, product_id)][lot_id] = qty
        return dict(result)

    # ──────────────────────────────────────────────
    # Dry run + reporting
    # ──────────────────────────────────────────────

    @api.model
    def dry_run(self):
        """Run classification and return formatted report (no writes)."""
        stats = self._classify_materials()
        report = self._format_report(stats, commit_applied=False)
        _logger.info('Phase 3 dry-run:\n%s', report)
        return {'stats': stats, 'report': report}

    @api.model
    def _format_report(self, stats, commit_applied=False):
        lines = []
        lines.append('=' * 66)
        lines.append('ESFSM PER-LOT ALLOCATION MIGRATION — {}'.format(
            'COMMITTED' if commit_applied else 'DRY RUN'
        ))
        lines.append('=' * 66)
        lines.append('')
        lines.append('[CLASSIFICATION]')
        lines.append('  Tracked materials (taken > 0):   {}'.format(stats['tracked_total']))
        lines.append('  Untracked (no allocation needed): {}'.format(stats['untracked']))
        lines.append('  Already migrated (allocations):  {}'.format(stats['already_migrated']))
        lines.append('')
        lines.append('[BUCKETS]')
        lines.append('  Clean (1:1 lot→material):     {} → will create 1 allocation each'.format(
            len(stats['clean'])))
        lines.append('  Multi-lot (unambiguous):      {} → will create N allocations each'.format(
            len(stats['multi_lot'])))
        lines.append('  Ambiguous (need resolution):  {} combos, {} material lines'.format(
            len(stats['ambiguous']),
            sum(len(c['material_ids']) for c in stats['ambiguous'])))
        lines.append('  Historical gap (no lot data): {} → flagged with lot_allocation_historical_gap=True'.format(
            len(stats['gap'])))
        lines.append('')

        total_new_allocs = len(stats['clean']) + sum(
            len(x['lots']) for x in stats['multi_lot'])
        lines.append('[WRITES]')
        lines.append('  New allocations to create:     {}'.format(total_new_allocs))
        lines.append('  Materials to mark as gap:      {}'.format(len(stats['gap'])))
        lines.append('  Legacy lot_id archived to JSON: all of above')
        lines.append('')
        if stats['ambiguous']:
            lines.append('[NEXT STEPS AFTER COMMIT]')
            lines.append('  Open Resolution Wizard to handle {} ambiguous combos.'.format(
                len(stats['ambiguous'])))
            lines.append('  Path: Settings → ESFSM Stock → Ambiguous Resolution')
        lines.append('=' * 66)
        return '\n'.join(lines)

    # ──────────────────────────────────────────────
    # Migration execution
    # ──────────────────────────────────────────────

    @api.model
    def migrate(self, commit=False):
        """Run migration. If commit=False, acts as dry-run.
        Returns {'stats': {...}, 'report': '...', 'committed': bool}."""
        if not self.env['esfsm.job.material']._is_per_lot_enabled():
            raise UserError(_(
                'Feature flag esfsm_stock.per_lot_allocations_enabled мора да биде ON '
                'пред извршување на Phase 3 migration.'
            ))

        stats = self._classify_materials()

        if not commit:
            report = self._format_report(stats, commit_applied=False)
            return {'stats': stats, 'report': report, 'committed': False}

        # Commit phase
        Material = self.env['esfsm.job.material']
        Allocation = self.env['esfsm.job.material.lot']
        created_allocs = 0
        flagged_gap = 0

        # 1. Clean cases — 1 allocation per material
        for entry in stats['clean']:
            material = Material.browse(entry['material_id'])
            lot_id = entry['lot_id']
            # Use the smaller of picking qty vs material.taken_qty
            # (picking might have more than material claims, or vice versa — trust material)
            qty = material.taken_qty
            self._snapshot_legacy(material)
            Allocation.with_context(skip_allocation_sum_check=True).create({
                'material_id': material.id,
                'lot_id': lot_id,
                'taken_qty': qty,
                'used_qty': min(material.used_qty, qty),
                'returned_qty': min(material.returned_qty, qty - min(material.used_qty, qty)),
            })
            created_allocs += 1

        # 2. Multi-lot cases — distribute material totals proportionally across lots
        for entry in stats['multi_lot']:
            material = Material.browse(entry['material_id'])
            total_picking_qty = sum(q for _, q in entry['lots'])
            if total_picking_qty <= 0:
                continue
            self._snapshot_legacy(material)
            # Proportional split with residual reconciliation: per-lot values are
            # rounded to UoM precision so per-lot sums tie exactly to the material
            # scalar (otherwise 1/3+1/3+1/3 of 10 → 9.99 trips _check_lot_sum_matches)
            # while preserving used+returned<=taken per lot (matters under coarse
            # UoM rounding — see _split_proportional).
            rounding = material._rounding()
            distributed = self._split_proportional(
                entry['lots'], total_picking_qty,
                material_taken=material.taken_qty,
                material_used=material.used_qty,
                material_returned=material.returned_qty,
                rounding=rounding,
            )
            for d in distributed:
                Allocation.with_context(skip_allocation_sum_check=True).create({
                    'material_id': material.id,
                    'lot_id': d['lot_id'],
                    'taken_qty': d['taken_qty'],
                    'used_qty': d['used_qty'],
                    'returned_qty': d['returned_qty'],
                })
                created_allocs += 1

        # 3. Historical gap — flag only
        for mid in stats['gap']:
            material = Material.browse(mid)
            self._snapshot_legacy(material)
            material.with_context(skip_allocation_sum_check=True).write({
                'lot_allocation_historical_gap': True,
            })
            flagged_gap += 1

        report = self._format_report(stats, commit_applied=True)
        report += '\n\n[WRITES APPLIED]\n  Allocations created: {}\n  Gap flags set:       {}'.format(
            created_allocs, flagged_gap,
        )
        _logger.warning('Phase 3 migration COMMITTED: %s allocations, %s gap flags',
                        created_allocs, flagged_gap)
        return {
            'stats': stats,
            'report': report,
            'committed': True,
            'allocations_created': created_allocs,
            'gap_flagged': flagged_gap,
        }

    @api.model
    def _split_proportional(self, lots, total_picking_qty,
                            material_taken, material_used, material_returned,
                            rounding):
        """Split material scalars (taken/used/returned) proportionally across
        `lots` (a list of (lot_id, picking_qty) pairs) by picking ratio, with
        residual reconciliation so per-lot sums tie EXACTLY to each scalar.

        Each per-lot value is rounded to UoM precision via a capped
        largest-remainder apportionment, so the per-lot sums reconcile to each
        material scalar AND every lot preserves the allocation-model invariant
        ``used_qty + returned_qty <= taken_qty`` (within UoM rounding). The
        latter matters under COARSE UoM rounding (> 0.01, e.g. pack-of-50
        rounding 1.0): a naïve per-field rounding + largest-sink residual can
        round `used`/`returned` up on a small lot past its `taken`, which would
        trip esfsm.job.material.lot._check_used_quantity /
        _check_returned_quantity and roll back the whole migration/resolution.

        Mechanics:
          * ``taken`` is apportioned by picking ratio with no cross-field cap
            (largest-remainder hands the rounding leftover to the lots with the
            biggest fractional part — in the common 2-decimal case this is the
            largest lot, matching the previous largest-sink behaviour).
          * ``used`` is apportioned with each lot capped at its ``taken``.
          * ``returned`` is apportioned with each lot capped at its remaining
            ``taken - used`` headroom.
        When a lot's cap is binding, the residual spills to the next lot with
        headroom (most headroom first), so the invariant always holds while the
        per-field totals still reconcile exactly.

        Returns a list of dicts: [{'lot_id', 'taken_qty', 'used_qty',
        'returned_qty'}, ...] in input order. Returns [] if total_picking_qty<=0.
        """
        rounding = rounding or 0.001
        if total_picking_qty <= 0 or not lots:
            return []

        rows = [{'lot_id': lot_id, 'picking_qty': picking_qty}
                for lot_id, picking_qty in lots]
        weights = [r['picking_qty'] for r in rows]

        # taken: no cross-field constraint → uncapped proportional apportionment.
        self._apportion_capped(rows, 'taken_qty', material_taken,
                               weights, None, rounding)
        # used: cap each lot at its taken_qty (used <= taken).
        self._apportion_capped(
            rows, 'used_qty', material_used, weights,
            [r['taken_qty'] for r in rows], rounding)
        # returned: cap each lot at remaining headroom (returned <= taken - used).
        self._apportion_capped(
            rows, 'returned_qty', material_returned, weights,
            [float_round(r['taken_qty'] - r['used_qty'],
                         precision_rounding=rounding) for r in rows],
            rounding)

        # Drop the helper-only picking_qty key before returning.
        for r in rows:
            r.pop('picking_qty', None)
        return rows

    @api.model
    def _apportion_capped(self, rows, field, total, weights, caps, rounding):
        """Write `field` on each row in `rows` so the values are a UoM-rounded
        apportionment of `total` by `weights`, optionally bounded per-row by
        `caps`, with the per-row sum reconciling EXACTLY to float_round(total).

        Largest-remainder method: floor each lot's ideal share to UoM units
        (clamped to its cap), then hand out the remaining whole rounding-steps
        to the lots with the biggest fractional remainder that still have cap
        headroom (or claw back over-assignment from the smallest remainders).
        `caps=None` means uncapped. Mutates `rows` in place.
        """
        n = len(rows)
        total_weight = sum(weights)
        if float_is_zero(total, precision_rounding=rounding) or total_weight <= 0:
            for r in rows:
                r[field] = 0.0
            return

        # Work in integer rounding-units to keep the residual arithmetic exact.
        target_units = int(round(total / rounding))
        if caps is None:
            cap_units = [target_units] * n
        else:
            cap_units = [max(0, int(round(caps[i] / rounding))) for i in range(n)]

        ideal = [total * weights[i] / total_weight for i in range(n)]
        floor_units = [
            min(int(math.floor(ideal[i] / rounding + 1e-9)), cap_units[i])
            for i in range(n)
        ]
        frac = [
            ideal[i] / rounding - math.floor(ideal[i] / rounding + 1e-9)
            for i in range(n)
        ]
        remaining = target_units - sum(floor_units)

        # Hand out leftover steps to the biggest remainders that have headroom.
        up_order = sorted(range(n), key=lambda i: (-frac[i], -weights[i], i))
        while remaining > 0:
            progressed = False
            for i in up_order:
                if remaining <= 0:
                    break
                if floor_units[i] < cap_units[i]:
                    floor_units[i] += 1
                    remaining -= 1
                    progressed = True
            if not progressed:
                # All caps saturated — only happens if `total` exceeds the sum of
                # caps, i.e. the scalar is globally infeasible (callers guarantee
                # used<=taken and used+returned<=taken, so this is unreachable).
                break

        # Claw back any over-assignment (cap-flooring can overshoot the target).
        down_order = sorted(range(n), key=lambda i: (frac[i], weights[i], i))
        while remaining < 0:
            progressed = False
            for i in down_order:
                if remaining >= 0:
                    break
                if floor_units[i] > 0:
                    floor_units[i] -= 1
                    remaining += 1
                    progressed = True
            if not progressed:
                break

        for i in range(n):
            rows[i][field] = float_round(
                floor_units[i] * rounding, precision_rounding=rounding)

    @api.model
    def _snapshot_legacy(self, material):
        """Save lot_id to lot_id_legacy_archive JSON for reversibility."""
        if not material.lot_id_legacy_archive:
            material.with_context(skip_allocation_sum_check=True).write({
                'lot_id_legacy_archive': {
                    'lot_id': material.lot_id.id if material.lot_id else None,
                    'lot_name': material.lot_id.name if material.lot_id else None,
                    'archived_at': fields.Datetime.to_string(fields.Datetime.now()),
                },
            })

    # ──────────────────────────────────────────────
    # Ambiguous-combo analysis (Phase 3 post-commit)
    # ──────────────────────────────────────────────

    @api.model
    def classify_ambiguous_by_shortage(self):
        """Split remaining ambiguous combos into 3 buckets based on whether
        the picking lot history can cover the total material_taken.

        Returns:
          {
            'resolvable_exact':    [...],  # sum_lot == sum_taken (can fully distribute)
            'resolvable_surplus':  [...],  # sum_lot > sum_taken (more lots than needed)
            'shortage':            [...],  # sum_lot < sum_taken (must mark as gap)
            'no_lot_history':      [...],  # sum_lot == 0 (no picking data at all)
          }
        Each entry: {
            'job_id', 'product_id', 'job_name', 'product_code',
            'material_ids', 'total_taken', 'total_lot', 'shortage',
        }
        """
        stats = self._classify_materials()
        Material = self.env['esfsm.job.material']
        Job = self.env['esfsm.job']
        Product = self.env['product.product']

        buckets = {
            'resolvable_exact': [],
            'resolvable_surplus': [],
            'shortage': [],
            'no_lot_history': [],
        }

        for combo in stats['ambiguous']:
            materials = Material.browse(combo['material_ids'])
            # Skip already-resolved combos
            if all(m.lot_allocation_ids or m.lot_allocation_historical_gap
                   for m in materials):
                continue
            lot_qtys = self._get_picking_lot_qtys(materials[:1])
            total_taken = sum(materials.mapped('taken_qty'))
            total_lot = sum(lot_qtys.values())
            rounding = materials[0].product_uom_id.rounding or 0.001
            cmp = __import__('odoo').tools.float_compare(
                total_lot, total_taken, precision_rounding=rounding)

            entry = {
                'job_id': combo['job_id'],
                'job_name': Job.browse(combo['job_id']).name,
                'product_id': combo['product_id'],
                'product_code': Product.browse(combo['product_id']).default_code or '',
                'material_ids': combo['material_ids'],
                'material_count': len(materials),
                'total_taken': total_taken,
                'total_lot': total_lot,
                'shortage': max(total_taken - total_lot, 0.0),
                'lot_count': len(lot_qtys),
            }
            if total_lot == 0:
                buckets['no_lot_history'].append(entry)
            elif cmp == 0:
                buckets['resolvable_exact'].append(entry)
            elif cmp > 0:
                buckets['resolvable_surplus'].append(entry)
            else:
                buckets['shortage'].append(entry)

        return buckets

    @api.model
    def format_ambiguous_report(self):
        """Formatted human-readable report of all remaining ambiguous combos."""
        buckets = self.classify_ambiguous_by_shortage()
        lines = ['=' * 70,
                 'AMBIGUOUS COMBOS — SHORTAGE CLASSIFICATION',
                 '=' * 70]
        for label, entries in [
            ('RESOLVABLE — EXACT MATCH (sum_lot == sum_taken)', buckets['resolvable_exact']),
            ('RESOLVABLE — SURPLUS (sum_lot > sum_taken)', buckets['resolvable_surplus']),
            ('SHORTAGE (must Mark as Gap)', buckets['shortage']),
            ('NO LOT HISTORY (must Mark as Gap)', buckets['no_lot_history']),
        ]:
            lines.append('')
            lines.append(f'[{label}] — {len(entries)} combos')
            for e in sorted(entries, key=lambda x: -x['shortage'])[:10]:
                lines.append(
                    f"  {e['job_name']} × {e['product_code']}: "
                    f"{e['material_count']} mats, taken={e['total_taken']:.1f}, "
                    f"lot={e['total_lot']:.1f}, shortage={e['shortage']:.1f}, "
                    f"lots={e['lot_count']}"
                )
            if len(entries) > 10:
                lines.append(f'  ... and {len(entries) - 10} more')
        total_shortage = sum(e['shortage'] for e in buckets['shortage']) \
                       + sum(e['total_taken'] for e in buckets['no_lot_history'])
        lines.append('')
        lines.append(f'[TOTAL UNBACKED QUANTITY (must gap)] {total_shortage:.1f}')
        lines.append('=' * 70)
        return '\n'.join(lines)

    @api.model
    def mark_shortage_combos_as_gap(self):
        """Bulk-mark all combos with shortage>0 or no lot history as gap.
        Safe: leaves resolvable combos (exact + surplus) for manual wizard."""
        buckets = self.classify_ambiguous_by_shortage()
        targets = buckets['shortage'] + buckets['no_lot_history']
        return self._bulk_gap_combos(targets, label='shortage')

    @api.model
    def mark_all_ambiguous_as_gap(self):
        """Bulk-mark ALL remaining ambiguous combos as gap, regardless of
        whether picking history is sufficient. Use when the ambiguous records
        are entirely historical (pre-lot-tracking) and the lot data attached
        to them is incidental rather than meaningful."""
        buckets = self.classify_ambiguous_by_shortage()
        targets = (buckets['shortage'] + buckets['no_lot_history']
                   + buckets['resolvable_exact'] + buckets['resolvable_surplus'])
        return self._bulk_gap_combos(targets, label='all_ambiguous')

    @api.model
    def _bulk_gap_combos(self, combo_entries, label='bulk'):
        """Internal bulk-gap executor. Skips materials already resolved."""
        Material = self.env['esfsm.job.material']
        flagged = 0
        for entry in combo_entries:
            for mid in entry['material_ids']:
                material = Material.browse(mid)
                if material.lot_allocation_ids or material.lot_allocation_historical_gap:
                    continue
                self._snapshot_legacy(material)
                material.with_context(skip_allocation_sum_check=True).write({
                    'lot_allocation_historical_gap': True,
                })
                flagged += 1
        _logger.warning(
            'Bulk-marked %s materials as historical_gap across %s combos (mode=%s)',
            flagged, len(combo_entries), label,
        )
        return {'flagged': flagged, 'combos': len(combo_entries)}

    # ──────────────────────────────────────────────
    # Rollback
    # ──────────────────────────────────────────────

    @api.model
    def rollback(self):
        """Restore lot_id from lot_id_legacy_archive and delete all allocations.
        Use with EXTREME caution — Phase 4 drops this field; point of no return."""
        Material = self.env['esfsm.job.material']
        archived = Material.search([('lot_id_legacy_archive', '!=', False)])
        restored = 0
        for m in archived:
            data = m.lot_id_legacy_archive or {}
            m.with_context(skip_allocation_sum_check=True).write({
                'lot_id': data.get('lot_id'),
                'lot_allocation_historical_gap': False,
                'lot_id_legacy_archive': False,
            })
            m.lot_allocation_ids.with_context(skip_allocation_sum_check=True).unlink()
            restored += 1
        _logger.warning('Phase 3 migration ROLLED BACK: %s materials restored', restored)
        return {'restored': restored}
