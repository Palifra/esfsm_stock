# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo import api, models, fields, _


class FleetVehicle(models.Model):
    _inherit = 'fleet.vehicle'

    stock_location_id = fields.Many2one(
        'stock.location',
        string='Stock Location',
        readonly=True,
        help='Stock location automatically created for this vehicle to track materials'
    )

    @api.model_create_multi
    def create(self, vals_list):
        """Override create to automatically create stock location for each vehicle"""
        vehicles = super().create(vals_list)

        # Get or create parent location WH/VEH (Vehicles)
        parent_location = self.env.ref('esfsm_stock.stock_location_vehicles', raise_if_not_found=False)
        if not parent_location:
            # Fallback: find WH/Stock
            parent_location = self.env['stock.location'].search([
                ('usage', '=', 'internal'),
                ('name', '=', 'Stock')
            ], limit=1)

        for vehicle in vehicles:
            # Create unique stock location for this vehicle
            location_name = f"Возило - {vehicle.license_plate or vehicle.name}"

            location = self.env['stock.location'].create({
                'name': location_name,
                'usage': 'internal',
                'location_id': parent_location.id if parent_location else False,
                'company_id': vehicle.company_id.id or False,
            })

            vehicle.stock_location_id = location.id

        return vehicles

    def write(self, vals):
        """Update location name if vehicle name/license changes"""
        res = super().write(vals)

        if 'name' in vals or 'license_plate' in vals:
            for vehicle in self:
                if vehicle.stock_location_id:
                    new_name = f"Возило - {vehicle.license_plate or vehicle.name}"
                    vehicle.stock_location_id.name = new_name

        return res
