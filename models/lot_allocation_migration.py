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
from collections import defaultdict

from odoo import api, models, fields, _
from odoo.exceptions import UserError

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

            # Find lots from pickings for this job+product
            lot_qtys = self._get_picking_lot_qtys(m)

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
    def _get_picking_lot_qtys(self, material):
        """Return {lot_id: qty} aggregated from done pickings on this job
        for the material's product. Keyed by (job_id, product_id) — all
        materials sharing these keys receive the same result, so callers
        in ambiguous combos must call this ONCE per combo (not per material)
        to avoid N× amplification.

        Accepts either a single record or a recordset (uses first)."""
        if hasattr(material, 'ensure_one') and len(material) > 1:
            material = material[:1]
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
            # Proportional split based on picking ratios
            material_taken = material.taken_qty
            material_used = material.used_qty
            material_returned = material.returned_qty
            for lot_id, picking_qty in entry['lots']:
                ratio = picking_qty / total_picking_qty
                alloc_taken = material_taken * ratio
                alloc_used = material_used * ratio
                alloc_returned = material_returned * ratio
                Allocation.with_context(skip_allocation_sum_check=True).create({
                    'material_id': material.id,
                    'lot_id': lot_id,
                    'taken_qty': alloc_taken,
                    'used_qty': alloc_used,
                    'returned_qty': alloc_returned,
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
