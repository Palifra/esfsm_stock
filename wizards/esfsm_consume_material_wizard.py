# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo import api, models, fields, _
from odoo.exceptions import ValidationError
from odoo.tools import float_compare


class EsfsmConsumeMaterialWizard(models.TransientModel):
    _name = 'esfsm.consume.material.wizard'
    _description = 'Wizard за потрошувачка на материјали'

    job_id = fields.Many2one(
        'esfsm.job',
        string='Работа',
        required=True,
        readonly=True,
    )
    line_ids = fields.One2many(
        'esfsm.consume.material.wizard.line',
        'wizard_id',
        string='Линии',
    )

    @api.model
    def default_get(self, fields_list):
        """Phase 4: per-allocation flat list.
        - Tracked materials with allocations → one row per (material, allocation)
        - Historical gap or untracked materials → one row per material (legacy fallback)
        """
        res = super().default_get(fields_list)

        job_id = self.env.context.get('active_id')
        if not job_id:
            return res

        job = self.env['esfsm.job'].browse(job_id)
        res['job_id'] = job_id

        lines = []
        for material in job.material_ids:
            total_available = (material.taken_qty - material.used_qty
                               - material.returned_qty)
            if total_available <= 0:
                continue

            # Case A: tracked material with allocations → per-allocation rows
            if material.lot_allocation_ids:
                for alloc in material.lot_allocation_ids:
                    avail = alloc.available_to_consume_qty
                    if avail <= 0:
                        continue
                    lines.append((0, 0, {
                        'material_line_id': material.id,
                        'allocation_id': alloc.id,
                        'product_id': material.product_id.id,
                        'product_uom_id': material.product_uom_id.id,
                        'lot_id': alloc.lot_id.id,
                        'taken_qty': alloc.taken_qty,
                        'already_used_qty': alloc.used_qty,
                        'already_returned_qty': alloc.returned_qty,
                        'available_to_consume': avail,
                        'planned_qty': material.planned_qty,
                        'consume_qty': 0.0,
                    }))
            else:
                # Case B: legacy / historical-gap / untracked → 1 row per material
                lines.append((0, 0, {
                    'material_line_id': material.id,
                    'allocation_id': False,
                    'product_id': material.product_id.id,
                    'product_uom_id': material.product_uom_id.id,
                    'lot_id': material.lot_id.id if material.lot_id else False,
                    'taken_qty': material.taken_qty,
                    'already_used_qty': material.used_qty,
                    'already_returned_qty': material.returned_qty,
                    'available_to_consume': total_available,
                    'planned_qty': material.planned_qty,
                    'consume_qty': 0.0,
                }))

        res['line_ids'] = lines
        return res

    def action_confirm(self):
        """Create Испратница picking and update allocations + material scalars."""
        self.ensure_one()

        lines_to_consume = self.line_ids.filtered(lambda l: l.consume_qty > 0)
        if not lines_to_consume:
            raise ValidationError(_('Нема материјали за потрошувачка.'))

        # Group by material for planned_qty validation + scalar rebuild
        material_deltas = {}  # material_id → total consume this action
        for line in lines_to_consume:
            mid = line.material_line_id.id
            material_deltas[mid] = material_deltas.get(mid, 0.0) + line.consume_qty

        for mid, delta in material_deltas.items():
            material = self.env['esfsm.job.material'].browse(mid)
            total_used = material.used_qty + delta
            rounding = material._rounding()
            if float_compare(total_used, material.planned_qty,
                             precision_rounding=rounding) > 0:
                raise ValidationError(_(
                    'Вкупно потрошено (%(total).2f) е поголемо од планираното '
                    '(%(planned).2f) за %(product)s.',
                    total=total_used, planned=material.planned_qty,
                    product=material.product_id.name,
                ))

        job = self.job_id
        picking_service = self.env['esfsm.stock.picking.service']
        picking = picking_service.create_delivery_picking(job, lines_to_consume)

        per_lot = self.env['esfsm.job.material']._is_per_lot_enabled()

        # Write per-allocation first, then rebuild material scalar
        touched_materials = set()
        for line in lines_to_consume:
            material = line.material_line_id
            touched_materials.add(material.id)
            if line.allocation_id:
                # Allocation-based consume
                line.allocation_id.with_context(
                    skip_allocation_sum_check=True,
                ).used_qty = line.allocation_id.used_qty + line.consume_qty
            # Material scalar is rebuilt from allocations (below) OR
            # updated directly for gap / untracked materials
            if not material.lot_allocation_ids and not line.allocation_id:
                # Legacy / gap path: material scalar += consume (no allocations)
                material.with_context(
                    skip_auto_picking=True,
                    skip_allocation_sum_check=True,
                ).used_qty = material.used_qty + line.consume_qty

        # Rebuild material scalars from allocation sums (Phase 4 source-of-truth)
        Material = self.env['esfsm.job.material']
        for mid in touched_materials:
            material = Material.browse(mid)
            if material.lot_allocation_ids:
                total_used = sum(material.lot_allocation_ids.mapped('used_qty'))
                material.with_context(
                    skip_auto_picking=True,
                    skip_allocation_sum_check=True,
                ).used_qty = total_used

        # Validate sums match after write (final integrity check)
        for mid in touched_materials:
            Material.browse(mid)._validate_allocation_sums()

        # Continue-to-return check
        remaining = sum(
            m.taken_qty - m.used_qty - m.returned_qty
            for m in job.material_ids
        )
        if remaining > 0:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Успешно'),
                    'message': _('Потрошено %d ставки. Остануваат %.2f за враќање.') % (
                        len(lines_to_consume), remaining
                    ),
                    'type': 'warning',
                    'sticky': False,
                    'next': {
                        'type': 'ir.actions.act_window',
                        'res_model': 'stock.picking',
                        'res_id': picking.id,
                        'view_mode': 'form',
                        'target': 'current',
                    }
                }
            }

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'res_id': picking.id,
            'view_mode': 'form',
            'target': 'current',
        }


class EsfsmConsumeMaterialWizardLine(models.TransientModel):
    _name = 'esfsm.consume.material.wizard.line'
    _description = 'Линија за потрошувачка на материјал'

    wizard_id = fields.Many2one(
        'esfsm.consume.material.wizard',
        required=True,
        ondelete='cascade',
    )
    material_line_id = fields.Many2one(
        'esfsm.job.material',
        string='Материјал',
        required=True,
        readonly=True,
    )
    allocation_id = fields.Many2one(
        'esfsm.job.material.lot',
        string='Алокација',
        readonly=True,
        help='Ако е поставено, трошењето го ажурира овој allocation запис. '
             'Ако е празно (historical gap), се ажурира само material.used_qty.',
    )
    product_id = fields.Many2one(
        'product.product',
        required=True,
        readonly=True,
    )
    product_uom_id = fields.Many2one(
        'uom.uom',
        required=True,
        readonly=True,
    )
    lot_id = fields.Many2one(
        'stock.lot',
        string='Лот',
        readonly=True,
        domain="[('product_id', '=', product_id)]",
    )
    product_tracking = fields.Selection(
        related='product_id.tracking',
        readonly=True,
    )
    taken_qty = fields.Float(
        string='Земено',
        readonly=True,
        digits='Product Unit of Measure',
    )
    already_used_qty = fields.Float(
        string='Веќе потрошено',
        readonly=True,
        digits='Product Unit of Measure',
    )
    already_returned_qty = fields.Float(
        string='Веќе вратено',
        readonly=True,
        digits='Product Unit of Measure',
    )
    planned_qty = fields.Float(
        string='Планирано',
        readonly=True,
        digits='Product Unit of Measure',
    )
    available_to_consume = fields.Float(
        string='Достапно',
        readonly=True,
        digits='Product Unit of Measure',
    )
    consume_qty = fields.Float(
        string='Потроши',
        digits='Product Unit of Measure',
    )

    @api.constrains('consume_qty', 'available_to_consume')
    def _check_consume_qty(self):
        for line in self:
            if line.consume_qty < 0:
                raise ValidationError(_('Количината не може да биде негативна.'))
            if line.material_line_id:
                rounding = line.material_line_id._rounding()
                if float_compare(line.consume_qty, line.available_to_consume,
                                 precision_rounding=rounding) > 0:
                    raise ValidationError(_(
                        'Количината за потрошувачка (%(qty)s) не може да биде поголема '
                        'од достапната (%(avail)s) за %(product)s',
                        qty=line.consume_qty, avail=line.available_to_consume,
                        product=line.product_id.name,
                    ))
