# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo import models, fields, _
from odoo.exceptions import UserError
from odoo.tools import float_compare, float_is_zero


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

        Distinguishes two cases correctly (fixes #3):
          - product.tracking == 'none': lot irrelevant, just set qty.
          - product.tracking in ('lot', 'serial'): lot MUST be provided;
            raises UserError if missing (defense-in-depth — prevents future
            regressions where callers forget to pass the lot).

        Args:
            move: stock.move record
            lot_id: stock.lot record or False
        """
        if move.product_id.tracking == 'none':
            move.quantity = move.product_uom_qty
            return

        if not lot_id:
            raise UserError(_(
                'Производ "%s" бара лот/сериски број, но не е наведен. '
                'Проверете ја материјалната линија или визардот.'
            ) % move.product_id.name)

        # For lot-tracked products, stamp the lot on move lines WITHOUT
        # inflating qty.
        if move.move_line_ids:
            lines = move.move_line_ids
            if len(lines) == 1:
                lines.lot_id = lot_id
                lines.quantity = move.product_uom_qty
            else:
                # Multiple reserved lines (multi-lot / FEFO split). action_assign()
                # already reserved the correct per-line quantities — do NOT touch
                # them (overwriting them was the inflation bug). Only stamp the
                # caller lot on lines that don't already carry one.
                for ml in lines:
                    if not ml.lot_id:
                        ml.lot_id = lot_id
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
                'esfsm_material_line_id': line_data['material_line_id'].id
                                          if line_data.get('material_line_id') else False,
            })
            
            # Handle lot tracking if applicable
            lot_id = line_data.get('lot_id', False)
            if lot_id:
                self._handle_lot_tracking(move, lot_id)
        
        # Validate picking
        picking.action_confirm()
        picking.action_assign()

        # Finalize done quantities (distribute remaining demand; guard negative
        # vehicle/technician stock). Extracted so the logic is unit-testable.
        self._finalize_picking_quantities(picking)

        picking.button_validate()

        return picking

    def _finalize_picking_quantities(self, picking):
        """Set the done quantity on every move/line WITHOUT inflating demand.

        Replaces the old finalizer which forced ``move.product_uom_qty`` onto
        EVERY unfilled move line. When action_assign() reserved a move across
        several lots/locations (K move lines), that produced
        ``done = K * demand`` (multi-lot inflation) and, for internal vehicle
        sources, drove stock negative because no availability was checked.

        New behavior, per move:

        1. Negative-stock guard (EARLY, FRIENDLY check — not the sole defense).
           If the source is an *internal* location (vehicle/technician), the
           on-hand qty at that location must cover the demand; otherwise raise a
           localized UserError. NOTE: this uses physical ``qty_available`` (not
           reservation-aware ``free_qty``) and is NOT lot-filtered, so it catches
           the common whole-product shortfall early with a clear message but is
           not airtight. The authoritative backstop against negative stock is OCA
           ``stock_no_negative`` (active in production; it self-disables only
           under --test-enable, which is exactly why the test for this guard is
           load-bearing). Warehouse-sourced reverse takes also flow through here,
           but the warehouse normally has stock, so this simply confirms it.

        2. No reserved lines -> set the single done quantity to the demand
           (non-tracked or unreserved moves).

        3. Reserved lines -> distribute only the REMAINING demand
           (``demand - already-assigned``) across the unfilled (zero-qty) lines.
           For our flows there is normally at most one unfilled line after the
           create-branch line, but the loop is written to be correct for several
           unfilled lines too: each unfilled line takes up to the whole
           remainder, and once the remainder is exhausted the rest stay at zero.
           We intentionally do NOT spread a fixed share per line — Odoo's
           reservation already sized the genuinely reserved lines, and any
           leftover unreserved line should absorb exactly what is missing, never
           a full extra demand. (If several unfilled lines exist they are filled
           in order; the first one normally consumes the entire remainder, which
           is the real-world case for a single create-branch line.)

        Args:
            picking: stock.picking record (already confirmed + assigned)
        """
        for move in picking.move_ids:
            rounding = move.product_uom.rounding
            demand = move.product_uom_qty

            # (1) Negative-stock guard for internal (vehicle/technician) sources.
            if move.location_id.usage == 'internal':
                available = move.product_id.with_context(
                    location=move.location_id.id).qty_available
                if float_compare(available, demand,
                                 precision_rounding=rounding) < 0:
                    raise UserError(_(
                        'Недоволно залиха за "%s" на локација %s: '
                        'бара %s, достапно %s.'
                    ) % (move.product_id.name,
                         move.location_id.display_name,
                         demand, available))

            # (2) No reserved lines -> single done quantity = demand.
            if not move.move_line_ids:
                move.quantity = demand
                continue

            # (3) Distribute only the REMAINING demand across unfilled lines.
            assigned = sum(move.move_line_ids.mapped('quantity'))
            remaining = demand - assigned
            if float_compare(remaining, 0.0,
                             precision_rounding=rounding) > 0:
                for ml in move.move_line_ids:
                    if float_is_zero(remaining, precision_rounding=rounding):
                        break
                    if float_is_zero(ml.quantity, precision_rounding=rounding):
                        # This unfilled line absorbs the whole remainder.
                        ml.quantity = remaining
                        remaining = 0.0

    def _normalize_wizard_lines(self, wizard_lines, qty_field):
        """Turn a wizard-line recordset into the normalized ``material_lines``
        dict list consumed by ``create_*_picking_from_lines``.

        Each wizard exposes its quantity under a different field name
        (``take_qty`` / ``consume_qty`` / ``return_qty``); ``qty_field`` selects
        it. Lines with a zero/negative quantity are skipped (same behavior the
        inline loops had previously).

        Args:
            wizard_lines: recordset of wizard lines.
            qty_field: str, the quantity field name on the line.

        Returns:
            list[dict] with keys material_line_id, product_id, product_uom_id,
            quantity, lot_id.
        """
        material_lines = []
        for line in wizard_lines:
            qty = getattr(line, qty_field)
            if qty > 0:
                material_lines.append({
                    'material_line_id': line.material_line_id,
                    'product_id': line.product_id,
                    'product_uom_id': line.product_uom_id,
                    'quantity': qty,
                    'lot_id': line.lot_id if line.lot_id else False,
                })
        return material_lines

    def create_reverse_picking(self, job, wizard_lines):
        """
        Create Реверс picking (warehouse → technician/vehicle).
        Used when taking materials from warehouse.

        Thin wrapper: normalizes the wizard lines (``take_qty``) and delegates to
        ``create_reverse_picking_from_lines`` — the single canonical path also
        used by ``esfsm.job.material.apply_take`` and (next task) the REST API.

        Args:
            job: esfsm.job record
            wizard_lines: recordset of wizard lines with fields:
                         material_line_id, product_id, product_uom_id, take_qty, lot_id

        Returns:
            stock.picking: Created picking record
        """
        material_lines = self._normalize_wizard_lines(wizard_lines, 'take_qty')
        return self.create_reverse_picking_from_lines(job, material_lines)

    def create_reverse_picking_from_lines(self, job, material_lines):
        """Canonical Реверс builder (warehouse → technician/vehicle).

        Accepts a pre-normalized ``material_lines`` dict list so it can be driven
        by EITHER wizard lines (via ``create_reverse_picking``) OR a single
        (material, qty, lot) tuple (via ``apply_take``) without a wizard record.

        Args:
            job: esfsm.job record
            material_lines: list of dicts with keys material_line_id, product_id,
                           product_uom_id, quantity, lot_id (optional).

        Returns:
            stock.picking: Created picking record (empty recordset if no lines).
        """
        if not material_lines:
            return self.env['stock.picking']

        # Get picking type
        picking_type = self._get_picking_type('Реверс', job.company_id.id, 'internal')

        # Get locations
        warehouse = self.env['stock.warehouse'].search([
            ('company_id', '=', job.company_id.id)
        ], limit=1)
        source_location = warehouse.lot_stock_id if warehouse else self.env.ref('stock.stock_location_stock')
        dest_location = job._get_source_location()

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

        Thin wrapper: normalizes the wizard lines (``consume_qty``) and delegates
        to ``create_delivery_picking_from_lines``.

        Args:
            job: esfsm.job record
            wizard_lines: recordset of wizard lines with fields:
                         material_line_id, product_id, product_uom_id, consume_qty, lot_id

        Returns:
            stock.picking: Created picking record
        """
        material_lines = self._normalize_wizard_lines(wizard_lines, 'consume_qty')
        return self.create_delivery_picking_from_lines(job, material_lines)

    def create_delivery_picking_from_lines(self, job, material_lines):
        """Canonical Испратница builder (technician/vehicle → customer).

        Args:
            job: esfsm.job record
            material_lines: list of dicts (see create_reverse_picking_from_lines).

        Returns:
            stock.picking: Created picking record (empty recordset if no lines).
        """
        if not material_lines:
            return self.env['stock.picking']

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

        Thin wrapper: normalizes the wizard lines (``return_qty``) and delegates
        to ``create_return_picking_from_lines``.

        Args:
            job: esfsm.job record
            wizard_lines: recordset of wizard lines with fields:
                         material_line_id, product_id, product_uom_id, return_qty, lot_id

        Returns:
            stock.picking: Created picking record
        """
        material_lines = self._normalize_wizard_lines(wizard_lines, 'return_qty')
        return self.create_return_picking_from_lines(job, material_lines)

    def create_return_picking_from_lines(self, job, material_lines):
        """Canonical Повратница builder (technician/vehicle → warehouse).

        Args:
            job: esfsm.job record
            material_lines: list of dicts (see create_reverse_picking_from_lines).

        Returns:
            stock.picking: Created picking record (empty recordset if no lines).
        """
        if not material_lines:
            return self.env['stock.picking']

        # Get picking type. eskon_reverse names the return type 'Повратница'
        # (NOT 'Враќање на Реверс', which was never created — the old lookup
        # silently fell back to a generic internal type, so every FSM return was
        # mis-classified as 'Интерни трансфери' instead of 'Повратница').
        picking_type = self._get_picking_type('Повратница', job.company_id.id, 'internal')

        # Get locations
        source_location = job._get_source_location()  # Technician/vehicle location
        # Return to the company's warehouse stock — the SAME warehouse реверс
        # issues from. We resolve it from the warehouse, NOT from
        # picking_type.default_location_dest_id: there are duplicate per-warehouse
        # 'Повратница' types and the name lookup (limit=1) can pick the wrong
        # warehouse's type, which would otherwise redirect returns to the wrong
        # стоваришен location.
        warehouse = self.env['stock.warehouse'].search([
            ('company_id', '=', job.company_id.id)
        ], limit=1)
        dest_location = warehouse.lot_stock_id if warehouse else self.env.ref('stock.stock_location_stock')

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
