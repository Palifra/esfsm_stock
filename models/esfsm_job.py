# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo import api, models, fields, _
from odoo.exceptions import ValidationError


class EsfsmJob(models.Model):
    _inherit = 'esfsm.job'

    material_ids = fields.One2many(
        'esfsm.job.material',
        'job_id',
        string='Материјали',
        help='Листа на материјали за оваа работа'
    )
    material_count = fields.Integer(
        string='Број на материјали',
        compute='_compute_material_count',
        store=True,
        help='Вкупен број на материјални ставки'
    )
    has_materials_to_take = fields.Boolean(
        string='Има материјали за превземање',
        compute='_compute_has_materials_to_take',
        store=True,
        help='Дали има материјали со планирана количина поголема од земената'
    )
    has_materials_to_consume = fields.Boolean(
        string='Има материјали за потрошувачка',
        compute='_compute_has_materials_to_consume',
        store=True,
        help='Дали има материјали земени но не потрошени/вратени'
    )
    has_materials_to_return = fields.Boolean(
        string='Има невратени материјали',
        compute='_compute_has_materials_to_return',
        store=True,
        help='Дали има материјали кои не се вратени (taken - used - returned > 0)'
    )
    materials_to_return_count = fields.Integer(
        string='Број на невратени материјали',
        compute='_compute_has_materials_to_return',
        store=True,
        help='Број на материјали кои треба да се вратат'
    )
    material_total = fields.Monetary(
        string='Вкупна вредност на материјали',
        compute='_compute_material_total',
        help='Вкупна вредност на искористени материјали'
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Валута',
        related='company_id.currency_id',
        readonly=True
    )

    @api.depends('material_ids')
    def _compute_material_count(self):
        """Count material lines"""
        for job in self:
            job.material_count = len(job.material_ids)

    @api.depends('material_ids.planned_qty', 'material_ids.taken_qty')
    def _compute_has_materials_to_take(self):
        """Check if there are materials with planned_qty > taken_qty"""
        for job in self:
            job.has_materials_to_take = any(
                m.planned_qty > m.taken_qty for m in job.material_ids
            )

    @api.depends('material_ids.taken_qty', 'material_ids.used_qty', 'material_ids.returned_qty')
    def _compute_has_materials_to_consume(self):
        """Check if there are taken materials that can be consumed"""
        for job in self:
            # Materials that are taken but not fully consumed/returned
            job.has_materials_to_consume = any(
                (m.taken_qty - m.used_qty - m.returned_qty) > 0
                for m in job.material_ids
            )

    @api.depends('material_ids.taken_qty', 'material_ids.used_qty', 'material_ids.returned_qty')
    def _compute_has_materials_to_return(self):
        """Check if there are materials that need to be returned"""
        for job in self:
            materials_to_return = job.material_ids.filtered(
                lambda m: (m.taken_qty - m.used_qty - m.returned_qty) > 0
            )
            job.has_materials_to_return = bool(materials_to_return)
            job.materials_to_return_count = len(materials_to_return)

    @api.depends('material_ids.price_subtotal')
    def _compute_material_total(self):
        """Sum total material cost"""
        for job in self:
            job.material_total = sum(job.material_ids.mapped('price_subtotal'))

    def _get_source_location(self):
        """
        Determine source location for materials using the Location Provider.

        Uses centralized priority logic from eskon_reverse.stock.location.provider:
        - Priority based on Settings: vehicle/employee/team first
        - Falls back through configured resources
        - Ultimate fallback to warehouse

        Returns:
            stock.location recordset
        """
        self.ensure_one()

        # Use the centralized Location Provider service
        provider = self.env['stock.location.provider']
        location = provider.get_fsm_location(self)

        if location:
            return location

        # Fallback: Field technicians location (for technicians without vehicle)
        if self.employee_ids:
            field_tech_location = self.env.ref('esfsm_stock.stock_location_field_technicians', raise_if_not_found=False)
            if field_tech_location:
                return field_tech_location

        # Ultimate fallback: Default warehouse stock location
        warehouse = self.env['stock.warehouse'].search([
            ('company_id', '=', self.company_id.id)
        ], limit=1)

        if warehouse:
            return warehouse.lot_stock_id

        return self.env.ref('stock.stock_location_stock', raise_if_not_found=False) or \
               self.env['stock.location'].search([('usage', '=', 'internal')], limit=1)

    def _get_destination_location(self):
        """
        Determine destination location for material returns
        Same priority as source location

        Returns:
            stock.location recordset
        """
        return self._get_source_location()

    def action_view_materials(self):
        """Open material lines for this job"""
        self.ensure_one()
        return {
            'name': _('Материјали'),
            'type': 'ir.actions.act_window',
            'res_model': 'esfsm.job.material',
            'view_mode': 'list,form',
            'domain': [('job_id', '=', self.id)],
            'context': {'default_job_id': self.id},
        }

    def action_add_materials(self):
        """Open wizard to add materials to job"""
        self.ensure_one()
        return {
            'name': _('Додади материјали'),
            'type': 'ir.actions.act_window',
            'res_model': 'esfsm.add.material.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {'default_job_id': self.id},
        }

    def action_return_materials(self):
        """Open wizard to return materials from job"""
        self.ensure_one()
        return {
            'name': _('Врати материјали'),
            'type': 'ir.actions.act_window',
            'res_model': 'esfsm.return.material.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {'default_job_id': self.id},
        }

    def action_take_materials(self):
        """Open wizard to take materials for job"""
        self.ensure_one()
        return {
            'name': _('Превземи материјали'),
            'type': 'ir.actions.act_window',
            'res_model': 'esfsm.take.material.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {'default_job_id': self.id, 'active_id': self.id},
        }

    def action_consume_materials(self):
        """Open wizard to consume materials from job"""
        self.ensure_one()
        return {
            'name': _('Потроши материјали'),
            'type': 'ir.actions.act_window',
            'res_model': 'esfsm.consume.material.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {'default_job_id': self.id, 'active_id': self.id},
        }


    def action_complete(self):
        """
        Override to validate that all materials are returned before completing job.
        Materials must either be:
        - Fully used (used_qty == taken_qty)
        - Or returned (returned_qty covers the difference)
        """
        self.ensure_one()

        # Check for unreturned materials
        unreturned_materials = self.material_ids.filtered(
            lambda m: m.available_to_return_qty > 0
        )

        if unreturned_materials:
            # Build detailed message with material names and quantities
            material_details = []
            for m in unreturned_materials:
                material_details.append(
                    f"• {m.product_id.name}: {m.available_to_return_qty} {m.product_uom_id.name}"
                )
            details_text = '\n'.join(material_details)

            raise ValidationError(_(
                'Не може да се заврши работата додека има невратени материјали!\n\n'
                'Следните материјали треба да се вратат или да се означат како искористени:\n'
                '%s\n\n'
                'Користете го копчето "Врати материјали" за да ги вратите невратените материјали, '
                'или ажурирајте ја "Искористена количина" ако материјалите се потрошени.'
            ) % details_text)

        return super().action_complete()
