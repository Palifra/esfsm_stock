# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

"""Manual resolution wizard for Phase 3 ambiguous cases.

An ambiguous combo is (job_id, product_id) where more than one material line
exists for the same product in the same job. Migration can't automatically
attribute picking lots to specific material lines — the user must choose.

Flow:
  1. Wizard default_get collects next unresolved ambiguous combo.
  2. Shows all material lines for this job+product + all lots from picking history.
  3. User fills in qty_per_lot per material line (like split UI).
  4. Confirm creates allocations matching user choices.
"""

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError, UserError


class EsfsmLotResolutionWizard(models.TransientModel):
    _name = 'esfsm.lot.resolution.wizard'
    _description = 'Wizard за мануелно resolution на ambiguous lot allocations'

    job_id = fields.Many2one('esfsm.job', string='Работа', readonly=True)
    product_id = fields.Many2one('product.product', string='Производ', readonly=True)
    line_ids = fields.One2many(
        'esfsm.lot.resolution.wizard.line',
        'wizard_id',
        string='Алокации',
    )
    remaining_combos = fields.Integer(
        string='Преостанати combos',
        readonly=True,
        help='Брoj of ambiguous combos still to resolve after this one.',
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)

        # Find the next unresolved ambiguous combo
        combo = self._find_next_ambiguous()
        if not combo:
            res['line_ids'] = []
            return res

        job_id, product_id, material_ids = combo
        res['job_id'] = job_id
        res['product_id'] = product_id

        # Collect materials + picking-history lots
        materials = self.env['esfsm.job.material'].browse(material_ids)
        migration = self.env['esfsm.lot.allocation.migration']
        lot_qtys = {}
        for m in materials:
            for lot_id, qty in migration._get_picking_lot_qtys(m).items():
                lot_qtys[lot_id] = lot_qtys.get(lot_id, 0.0) + qty
        # Deduplicate by summing (already done above — lot_qtys is per-combo)

        # Build cross-product rows: one wizard line per (material, lot) pair
        lines = []
        for material in materials:
            for lot_id, lot_total in lot_qtys.items():
                lines.append((0, 0, {
                    'material_id': material.id,
                    'lot_id': lot_id,
                    'material_taken_qty': material.taken_qty,
                    'lot_total_qty': lot_total,
                    'qty': 0.0,
                }))
        res['line_ids'] = lines

        # Count remaining combos
        stats = migration._classify_materials()
        res['remaining_combos'] = max(len(stats['ambiguous']) - 1, 0)
        return res

    @api.model
    def _find_next_ambiguous(self):
        """Return (job_id, product_id, material_ids) for next unresolved combo, or None."""
        stats = self.env['esfsm.lot.allocation.migration']._classify_materials()
        for combo in stats['ambiguous']:
            materials = self.env['esfsm.job.material'].browse(combo['material_ids'])
            # Skip if all materials in combo already have allocations
            if all(m.lot_allocation_ids for m in materials):
                continue
            return (combo['job_id'], combo['product_id'], combo['material_ids'])
        return None

    def action_resolve(self):
        """Create allocations from user input. Validate that per-material totals
        match material.taken_qty exactly (must fully account for the take)."""
        self.ensure_one()
        migration = self.env['esfsm.lot.allocation.migration']
        Allocation = self.env['esfsm.job.material.lot']

        # Group lines by material_id
        by_material = {}
        for line in self.line_ids:
            by_material.setdefault(line.material_id.id, []).append(line)

        # Validate each material's allocations sum to taken_qty
        for mid, lines in by_material.items():
            material = self.env['esfsm.job.material'].browse(mid)
            total = sum(l.qty for l in lines)
            rounding = material._rounding()
            from odoo.tools import float_compare
            if float_compare(total, material.taken_qty, precision_rounding=rounding) != 0:
                raise ValidationError(_(
                    'Сума на алокации (%(total)s) за материјал %(id)s не се совпаѓа '
                    'со taken_qty (%(taken)s). Мора да е еднакво.',
                    total=total, id=mid, taken=material.taken_qty,
                ))

        # Create allocations
        created = 0
        for mid, lines in by_material.items():
            material = self.env['esfsm.job.material'].browse(mid)
            migration._snapshot_legacy(material)
            # Distribute used/returned proportionally (based on alloc ratio)
            total_alloc = sum(l.qty for l in lines)
            for line in lines:
                if line.qty <= 0:
                    continue
                ratio = line.qty / total_alloc if total_alloc else 0
                Allocation.with_context(skip_allocation_sum_check=True).create({
                    'material_id': material.id,
                    'lot_id': line.lot_id.id,
                    'taken_qty': line.qty,
                    'used_qty': material.used_qty * ratio,
                    'returned_qty': material.returned_qty * ratio,
                })
                created += 1
            material.invalidate_recordset(['lot_allocation_ids',
                                           'taken_qty_per_lot_sum',
                                           'used_qty_per_lot_sum',
                                           'returned_qty_per_lot_sum'])
            material._validate_allocation_sums()

        # Return action to open next wizard (or close if done)
        next_combo = self._find_next_ambiguous()
        if next_combo:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'esfsm.lot.resolution.wizard',
                'view_mode': 'form',
                'target': 'new',
                'name': _('Следна ambiguous алокација'),
            }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Готово'),
                'message': _('Сите ambiguous combos се resolved (created=%s).') % created,
                'type': 'success',
                'sticky': False,
            },
        }


class EsfsmLotResolutionWizardLine(models.TransientModel):
    _name = 'esfsm.lot.resolution.wizard.line'
    _description = 'Линија за ambiguous resolution'

    wizard_id = fields.Many2one(
        'esfsm.lot.resolution.wizard',
        required=True,
        ondelete='cascade',
    )
    material_id = fields.Many2one(
        'esfsm.job.material',
        required=True,
        readonly=True,
    )
    lot_id = fields.Many2one(
        'stock.lot',
        required=True,
        readonly=True,
    )
    material_taken_qty = fields.Float(
        string='Material taken',
        readonly=True,
        digits='Product Unit of Measure',
    )
    lot_total_qty = fields.Float(
        string='Lot total qty',
        readonly=True,
        digits='Product Unit of Measure',
    )
    qty = fields.Float(
        string='Допиши',
        digits='Product Unit of Measure',
        help='Количина од овој лот за овој материјал.',
    )
