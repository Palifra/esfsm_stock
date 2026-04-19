# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo import api, models, fields, _
from odoo.exceptions import ValidationError
from odoo.tools import float_compare


class EsfsmReturnMaterialWizard(models.TransientModel):
    _name = 'esfsm.return.material.wizard'
    _description = 'Wizard за враќање материјали'

    job_id = fields.Many2one(
        'esfsm.job',
        string='Работа',
        required=True,
        readonly=True,
    )
    line_ids = fields.One2many(
        'esfsm.return.material.wizard.line',
        'wizard_id',
        string='Линии',
    )

    @api.model
    def default_get(self, fields_list):
        """Phase 4: per-allocation flat list (mirror of consume wizard)."""
        res = super().default_get(fields_list)

        job_id = self.env.context.get('active_id')
        if not job_id:
            return res

        job = self.env['esfsm.job'].browse(job_id)
        res['job_id'] = job_id

        lines = []
        for material in job.material_ids:
            total_available = material.available_to_return_qty
            if total_available <= 0:
                continue

            if material.lot_allocation_ids:
                for alloc in material.lot_allocation_ids:
                    avail = alloc.available_to_return_qty
                    if avail <= 0:
                        continue
                    lines.append((0, 0, {
                        'material_line_id': material.id,
                        'allocation_id': alloc.id,
                        'product_id': material.product_id.id,
                        'product_uom_id': material.product_uom_id.id,
                        'lot_id': alloc.lot_id.id,
                        'available_qty': avail,
                        'return_qty': 0.0,
                    }))
            else:
                # Legacy / historical-gap / untracked fallback
                lines.append((0, 0, {
                    'material_line_id': material.id,
                    'allocation_id': False,
                    'product_id': material.product_id.id,
                    'product_uom_id': material.product_uom_id.id,
                    'lot_id': material.lot_id.id if material.lot_id else False,
                    'available_qty': total_available,
                    'return_qty': 0.0,
                }))

        res['line_ids'] = lines
        return res

    def action_confirm(self):
        """Create return picking and update allocations + material scalars."""
        self.ensure_one()

        lines_to_return = self.line_ids.filtered(lambda l: l.return_qty > 0)
        if not lines_to_return:
            raise ValidationError(_('Нема материјали за враќање.'))

        job = self.job_id
        picking_service = self.env['esfsm.stock.picking.service']
        picking = picking_service.create_return_picking(job, lines_to_return)

        touched_materials = set()
        for line in lines_to_return:
            material = line.material_line_id
            touched_materials.add(material.id)
            if line.allocation_id:
                line.allocation_id.with_context(
                    skip_allocation_sum_check=True,
                ).returned_qty = line.allocation_id.returned_qty + line.return_qty
            if not material.lot_allocation_ids and not line.allocation_id:
                material.with_context(
                    skip_auto_picking=True,
                    skip_allocation_sum_check=True,
                ).returned_qty = material.returned_qty + line.return_qty

        # Rebuild material scalars from allocations
        Material = self.env['esfsm.job.material']
        for mid in touched_materials:
            material = Material.browse(mid)
            if material.lot_allocation_ids:
                total_returned = sum(material.lot_allocation_ids.mapped('returned_qty'))
                material.with_context(
                    skip_auto_picking=True,
                    skip_allocation_sum_check=True,
                ).returned_qty = total_returned

        # Integrity check
        for mid in touched_materials:
            Material.browse(mid)._validate_allocation_sums()

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'res_id': picking.id,
            'view_mode': 'form',
            'target': 'current',
        }


class EsfsmReturnMaterialWizardLine(models.TransientModel):
    _name = 'esfsm.return.material.wizard.line'
    _description = 'Линија за враќање материјал'

    wizard_id = fields.Many2one(
        'esfsm.return.material.wizard',
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
    available_qty = fields.Float(
        string='Достапно',
        readonly=True,
        digits='Product Unit of Measure',
    )
    return_qty = fields.Float(
        string='Количина за враќање',
        digits='Product Unit of Measure',
        required=True,
    )

    @api.constrains('return_qty', 'available_qty')
    def _check_return_qty(self):
        for line in self:
            if line.return_qty < 0:
                raise ValidationError(_('Количината не може да биде негативна.'))
            if line.material_line_id:
                rounding = line.material_line_id._rounding()
                if float_compare(line.return_qty, line.available_qty,
                                 precision_rounding=rounding) > 0:
                    raise ValidationError(_(
                        'Количината за враќање (%(qty)s) не може да биде поголема '
                        'од достапната (%(avail)s) за %(product)s',
                        qty=line.return_qty, avail=line.available_qty,
                        product=line.product_id.name,
                    ))
