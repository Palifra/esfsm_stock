# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo import api, models, fields, _


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

    @api.depends('material_ids.price_subtotal')
    def _compute_material_total(self):
        """Sum total material cost"""
        for job in self:
            job.material_total = sum(job.material_ids.mapped('price_subtotal'))

    def _get_source_location(self):
        """
        Determine source location for materials with priority logic:
        1. Team vehicle location (if job assigned to team)
        2. FIRST employee location (if job assigned to multiple employees)
        3. Field technicians location (for technicians without vehicle)
        4. Default warehouse location (ultimate fallback)

        Returns:
            stock.location recordset
        """
        self.ensure_one()

        # Priority 1: Team vehicle location
        if self.team_id and self.team_id.stock_location_id:
            return self.team_id.stock_location_id

        # Priority 2: FIRST employee's stock location
        if self.employee_ids:
            first_employee = self.employee_ids[0]
            if first_employee.stock_location_id:
                return first_employee.stock_location_id

        # Priority 3: Field technicians location (for technicians without vehicle)
        # This prevents materials from staying in main warehouse
        if self.employee_ids:
            field_tech_location = self.env.ref('esfsm_stock.stock_location_field_technicians', raise_if_not_found=False)
            if field_tech_location:
                return field_tech_location

        # Priority 4: Default warehouse stock location (ultimate fallback)
        warehouse = self.env['stock.warehouse'].search([
            ('company_id', '=', self.company_id.id)
        ], limit=1)

        if warehouse:
            return warehouse.lot_stock_id
        else:
            # Ultimate fallback: any internal location
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
            'view_mode': 'tree,form',
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
            'target': 'new',
            'context': {'default_job_id': self.id},
        }
