# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo import models, fields, _


class StockPickingService(models.AbstractModel):
    """
    Service class for creating stock pickings for ESFSM material operations.
    Centralizes all stock picking creation logic to eliminate code duplication.
    """
    _name = 'esfsm.stock.picking.service'
    _description = 'ESFSM Stock Picking Service'

    def _get_technician_name(self, job):
        """
        Get technician name with fallback logic.
        
        Priority:
        1. Material responsible person
        2. First assigned employee
        3. 'Непознат' (Unknown)
        
        Args:
            job: esfsm.job record
            
        Returns:
            str: Technician name
        """
        if job.material_responsible_id:
            return job.material_responsible_id.name
        elif job.employee_ids:
            return job.employee_ids[0].name
        else:
            return 'Непознат'

    def _get_picking_type(self, picking_type_name, company_id, fallback_code='internal'):
        """
        Find picking type by name with fallback to code.
        
        Args:
            picking_type_name (str): Name of picking type to search for
            company_id (int): Company ID
            fallback_code (str): Fallback picking type code (default: 'internal')
            
        Returns:
            stock.picking.type: Picking type record
        """
        picking_type = self.env['stock.picking.type'].search([
            ('name', '=', picking_type_name),
            ('company_id', '=', company_id)
        ], limit=1)
        
        if not picking_type:
            picking_type = self.env['stock.picking.type'].search([
                ('code', '=', fallback_code),
                ('company_id', '=', company_id)
            ], limit=1)
        
        return picking_type

    def _handle_lot_tracking(self, move, lot_id):
        """
        Handle lot/serial number tracking for a stock move.
        
        Args:
            move: stock.move record
            lot_id: stock.lot record or False
        """
        if not lot_id:
            # Non-lot tracked products
            move.quantity = move.product_uom_qty
            return
        
        # For lot-tracked products, set lot on move lines
        if move.move_line_ids:
            for move_line in move.move_line_ids:
                move_line.lot_id = lot_id
                move_line.quantity = move.product_uom_qty
        else:
            # If no move lines exist, create one with lot
            self.env['stock.move.line'].create({
                'move_id': move.id,
                'product_id': move.product_id.id,
                'product_uom_id': move.product_uom.id,
                'location_id': move.location_id.id,
                'location_dest_id': move.location_dest_id.id,
                'lot_id': lot_id.id,
                'quantity': move.product_uom_qty,
                'picking_id': move.picking_id.id,
            })

    def _create_picking_with_moves(self, job, picking_type, source_location, dest_location, 
                                   material_lines, origin_suffix):
        """
        Create stock picking with moves for material lines.
        
        Args:
            job: esfsm.job record
            picking_type: stock.picking.type record
            source_location: stock.location record
            dest_location: stock.location record
            material_lines: list of dicts with keys: material_line_id, product_id, 
                           product_uom_id, quantity, lot_id (optional)
            origin_suffix: str, suffix for origin field (e.g., 'Реверс', 'Испратница')
            
        Returns:
            stock.picking: Created picking record
        """
        technician_name = self._get_technician_name(job)
        
        # Create picking
        picking = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'location_id': source_location.id,
            'location_dest_id': dest_location.id,
            'esfsm_job_id': job.id,
            'partner_id': job.partner_id.id if origin_suffix == 'Испратница' else False,
            'origin': f"{job.name} - {origin_suffix} - {technician_name}",
        })
        
        # Create stock moves for each material line
        for line_data in material_lines:
            move = self.env['stock.move'].create({
                'name': f"{job.name} - {line_data['product_id'].name}",
                'product_id': line_data['product_id'].id,
                'product_uom_qty': line_data['quantity'],
                'product_uom': line_data['product_uom_id'].id,
                'picking_id': picking.id,
                'location_id': source_location.id,
                'location_dest_id': dest_location.id,
            })
            
            # Handle lot tracking if applicable
            lot_id = line_data.get('lot_id', False)
            if lot_id:
                self._handle_lot_tracking(move, lot_id)
        
        # Validate picking
        picking.action_confirm()
        picking.action_assign()
        
        # Set quantities done for all moves
        for move in picking.move_ids:
            if not move.move_line_ids:
                move.quantity = move.product_uom_qty
            else:
                for ml in move.move_line_ids:
                    if not ml.quantity:
                        ml.quantity = move.product_uom_qty
        
        picking.button_validate()
        
        return picking

    def create_reverse_picking(self, job, wizard_lines):
        """
        Create Реверс picking (warehouse → technician/vehicle).
        Used when taking materials from warehouse.
        
        Args:
            job: esfsm.job record
            wizard_lines: recordset of wizard lines with fields:
                         material_line_id, product_id, product_uom_id, take_qty, lot_id
            
        Returns:
            stock.picking: Created picking record
        """
        # Get picking type
        picking_type = self._get_picking_type('Реверс', job.company_id.id, 'internal')
        
        # Get locations
        warehouse = self.env['stock.warehouse'].search([
            ('company_id', '=', job.company_id.id)
        ], limit=1)
        source_location = warehouse.lot_stock_id if warehouse else self.env.ref('stock.stock_location_stock')
        dest_location = job._get_source_location()
        
        # Prepare material lines data
        material_lines = []
        for line in wizard_lines:
            if line.take_qty > 0:
                material_lines.append({
                    'material_line_id': line.material_line_id,
                    'product_id': line.product_id,
                    'product_uom_id': line.product_uom_id,
                    'quantity': line.take_qty,
                    'lot_id': line.lot_id if line.lot_id else False,
                })
        
        # Create picking
        picking = self._create_picking_with_moves(
            job, picking_type, source_location, dest_location,
            material_lines, 'Реверс'
        )
        
        # Post message to job chatter
        technician_name = self._get_technician_name(job)
        material_list = ', '.join([
            f"{line['product_id'].name} ({line['quantity']} {line['product_uom_id'].name})"
            for line in material_lines
        ])
        job.message_post(
            body=_('Реверс издаден на %s: %s - %s') % (
                technician_name, material_list, picking.name
            )
        )
        
        return picking

    def create_delivery_picking(self, job, wizard_lines):
        """
        Create Испратница picking (technician/vehicle → customer).
        Used when consuming materials on job site.
        
        Args:
            job: esfsm.job record
            wizard_lines: recordset of wizard lines with fields:
                         material_line_id, product_id, product_uom_id, consume_qty, lot_id
            
        Returns:
            stock.picking: Created picking record
        """
        # Get picking type (outgoing)
        picking_type = self._get_picking_type('Испратници', job.company_id.id, 'outgoing')
        if not picking_type:
            picking_type = self.env['stock.picking.type'].search([
                ('code', '=', 'outgoing'),
                ('company_id', '=', job.company_id.id)
            ], limit=1)
        
        # Get locations
        source_location = job._get_source_location()  # Vehicle/technician location
        dest_location = self.env.ref('stock.stock_location_customers')  # Customer/consumption
        
        # Prepare material lines data
        material_lines = []
        for line in wizard_lines:
            if line.consume_qty > 0:
                material_lines.append({
                    'material_line_id': line.material_line_id,
                    'product_id': line.product_id,
                    'product_uom_id': line.product_uom_id,
                    'quantity': line.consume_qty,
                    'lot_id': line.lot_id if line.lot_id else False,
                })
        
        # Create picking
        picking = self._create_picking_with_moves(
            job, picking_type, source_location, dest_location,
            material_lines, 'Испратница'
        )
        
        # Post message to job chatter
        technician_name = self._get_technician_name(job)
        job.message_post(
            body=_('Испратница од %s кон %s: %d материјали - %s') % (
                technician_name, job.partner_id.name, len(material_lines), picking.name
            )
        )
        
        return picking

    def create_return_picking(self, job, wizard_lines):
        """
        Create Повратница picking (technician/vehicle → warehouse).
        Used when returning unused materials.
        
        Args:
            job: esfsm.job record
            wizard_lines: recordset of wizard lines with fields:
                         material_line_id, product_id, product_uom_id, return_qty, lot_id
            
        Returns:
            stock.picking: Created picking record
        """
        # Get picking type
        picking_type = self._get_picking_type('Враќање на Реверс', job.company_id.id, 'internal')
        
        # Get locations
        source_location = job._get_source_location()  # Technician/vehicle location
        dest_location = picking_type.default_location_dest_id or self.env.ref('stock.stock_location_stock')
        
        # Prepare material lines data
        material_lines = []
        for line in wizard_lines:
            if line.return_qty > 0:
                material_lines.append({
                    'material_line_id': line.material_line_id,
                    'product_id': line.product_id,
                    'product_uom_id': line.product_uom_id,
                    'quantity': line.return_qty,
                    'lot_id': line.lot_id if line.lot_id else False,
                })
        
        # Create picking
        picking = self._create_picking_with_moves(
            job, picking_type, source_location, dest_location,
            material_lines, 'Повратница'
        )
        
        # Post message to job chatter
        technician_name = self._get_technician_name(job)
        job.message_post(
            body=_('Повратница од %s: %d материјали - %s') % (
                technician_name, len(material_lines), picking.name
            )
        )
        
        return picking
