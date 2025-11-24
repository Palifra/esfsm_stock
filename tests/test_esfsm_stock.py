# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo.tests import TransactionCase, tagged
from odoo.exceptions import ValidationError


@tagged('post_install', '-at_install', 'esfsm_stock')
class TestEsfsmStock(TransactionCase):
    """Test suite for ESFSM Stock module"""

    @classmethod
    def setUpClass(cls):
        """Set up test data"""
        super().setUpClass()

        # Create test partner
        cls.partner = cls.env['res.partner'].create({
            'name': 'Test Customer',
        })

        # Create test product
        cls.product = cls.env['product.product'].create({
            'name': 'Test Product',
            'type': 'consu',
            'standard_price': 100.0,
        })

        # Create fleet vehicle model (required for vehicles)
        cls.vehicle_model = cls.env['fleet.vehicle.model'].create({
            'name': 'Test Model',
            'brand_id': cls.env['fleet.vehicle.model.brand'].create({'name': 'Test Brand'}).id,
        })

        # Create test vehicle
        cls.vehicle = cls.env['fleet.vehicle'].create({
            'name': 'Test Vehicle',
            'license_plate': 'TEST-001',
            'model_id': cls.vehicle_model.id,
        })

        # Create test employee
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Test Technician',
        })

        # Create test team
        cls.team = cls.env['esfsm.team'].create({
            'name': 'Test Team',
            'member_ids': [(4, cls.employee.id)],
        })

        # Create test job
        cls.job = cls.env['esfsm.job'].create({
            'name': 'TEST-JOB-001',
            'partner_id': cls.partner.id,
            'scheduled_date_start': '2025-01-15 10:00:00',
        })

    def _create_material(self, **kwargs):
        """Helper to create material with default UoM"""
        values = {
            'job_id': self.job.id,
            'product_id': self.product.id,
            'product_uom_id': self.product.uom_id.id,
            'price_unit': self.product.standard_price,
        }
        values.update(kwargs)
        return self.env['esfsm.job.material'].create(values)


@tagged('post_install', '-at_install', 'esfsm_stock')
class TestMaterialLifecycle(TestEsfsmStock):
    """Test material lifecycle tracking"""

    def test_01_create_material_line(self):
        """Test creating material line"""
        material = self._create_material(
            planned_qty=10.0,
            taken_qty=8.0,
        )

        self.assertEqual(material.planned_qty, 10.0)
        self.assertEqual(material.taken_qty, 8.0)
        self.assertEqual(material.used_qty, 0.0)
        self.assertEqual(material.returned_qty, 0.0)
        self.assertEqual(material.available_to_return_qty, 8.0)

    def test_02_computed_available_to_return(self):
        """Test available_to_return_qty computation"""
        material = self._create_material(
            taken_qty=10.0,
            used_qty=6.0,
        )

        self.assertEqual(material.available_to_return_qty, 4.0)

        # Update used quantity
        material.write({'used_qty': 8.0})
        self.assertEqual(material.available_to_return_qty, 2.0)

    def test_03_price_subtotal_computation(self):
        """Test price_subtotal computation"""
        material = self._create_material(
            taken_qty=5.0,
            used_qty=5.0,
            price_unit=100.0,
        )

        expected_subtotal = 5.0 * 100.0
        self.assertEqual(material.price_subtotal, expected_subtotal)

    def test_04_used_qty_constraint(self):
        """Test used_qty cannot exceed taken_qty"""
        material = self._create_material(taken_qty=5.0)

        with self.assertRaises(ValidationError):
            material.write({'used_qty': 10.0})

    def test_05_returned_qty_constraint(self):
        """Test returned_qty cannot exceed available"""
        material = self._create_material(
            taken_qty=10.0,
            used_qty=6.0,
        )

        # Should allow returning up to 4.0
        material.write({'returned_qty': 4.0})
        self.assertEqual(material.returned_qty, 4.0)

        # Should fail when exceeding available
        with self.assertRaises(ValidationError):
            material.write({'returned_qty': 5.0})

    def test_06_material_count_on_job(self):
        """Test material_count computation on job"""
        self.assertEqual(self.job.material_count, 0)

        # Create first material
        self._create_material(taken_qty=5.0)
        self.assertEqual(self.job.material_count, 1)

        # Create second material
        product2 = self.env['product.product'].create({
            'name': 'Test Product 2',
            'type': 'consu',
        })
        self._create_material(
            product_id=product2.id,
            product_uom_id=product2.uom_id.id,
            taken_qty=3.0,
            price_unit=50.0,
        )
        self.assertEqual(self.job.material_count, 2)

    def test_07_material_total_on_job(self):
        """Test material_total computation on job"""
        self.assertEqual(self.job.material_total, 0.0)

        # Create materials
        self._create_material(
            taken_qty=5.0,
            used_qty=5.0,
            price_unit=100.0,
        )

        product2 = self.env['product.product'].create({
            'name': 'Test Product 2',
            'type': 'consu',
        })
        self._create_material(
            product_id=product2.id,
            product_uom_id=product2.uom_id.id,
            taken_qty=3.0,
            used_qty=3.0,
            price_unit=50.0,
        )

        expected_total = (5.0 * 100.0) + (3.0 * 50.0)
        self.assertEqual(self.job.material_total, expected_total)


@tagged('post_install', '-at_install', 'esfsm_stock')
class TestFleetStockIntegration(TestEsfsmStock):
    """Test fleet-stock integration"""

    def test_01_vehicle_auto_creates_location(self):
        """Test vehicle automatically creates stock location"""
        vehicle = self.env['fleet.vehicle'].create({
            'name': 'Auto Location Test',
            'license_plate': 'AUTO-001',
            'model_id': self.vehicle_model.id,
        })

        self.assertTrue(vehicle.stock_location_id)
        self.assertEqual(vehicle.stock_location_id.usage, 'internal')
        self.assertIn('AUTO-001', vehicle.stock_location_id.name)

    def test_02_vehicle_location_name_updates(self):
        """Test stock location name updates with vehicle"""
        vehicle = self.env['fleet.vehicle'].create({
            'name': 'Original Name',
            'license_plate': 'UPDATE-001',
            'model_id': self.vehicle_model.id,
        })

        original_location = vehicle.stock_location_id
        self.assertIn('UPDATE-001', original_location.name)

        # Update vehicle license plate
        vehicle.write({'license_plate': 'NEW-PLATE'})
        # Location name should update to include new license plate
        self.assertIn('NEW-PLATE', vehicle.stock_location_id.name)

    def test_03_vehicle_location_parent(self):
        """Test vehicle location has correct parent"""
        vehicle = self.env['fleet.vehicle'].create({
            'name': 'Parent Test',
            'license_plate': 'PARENT-001',
            'model_id': self.vehicle_model.id,
        })

        parent_location = self.env.ref('esfsm_stock.stock_location_vehicles')
        self.assertEqual(vehicle.stock_location_id.location_id, parent_location)

    def test_04_team_vehicle_field(self):
        """Test team can have vehicle assigned"""
        self.team.write({'vehicle_id': self.vehicle.id})
        self.assertEqual(self.team.vehicle_id, self.vehicle)

    def test_05_team_stock_location_from_vehicle(self):
        """Test team gets stock location from vehicle"""
        self.team.write({'vehicle_id': self.vehicle.id})
        self.assertEqual(self.team.stock_location_id, self.vehicle.stock_location_id)

    def test_06_employee_vehicle_field(self):
        """Test employee can have vehicle assigned"""
        self.employee.write({'vehicle_id': self.vehicle.id})
        self.assertEqual(self.employee.vehicle_id, self.vehicle)


@tagged('post_install', '-at_install', 'esfsm_stock')
class TestLocationPriorityLogic(TestEsfsmStock):
    """Test stock location priority logic"""

    def test_01_team_vehicle_location_priority(self):
        """Test team vehicle location has highest priority"""
        # Assign vehicle to team
        self.team.write({'vehicle_id': self.vehicle.id})
        self.job.write({'team_id': self.team.id})

        source_location = self.job._get_source_location()
        self.assertEqual(source_location, self.vehicle.stock_location_id)

    def test_02_technician_vehicle_location_priority(self):
        """Test technician vehicle location is second priority"""
        # Assign vehicle to employee
        self.employee.write({'vehicle_id': self.vehicle.id})
        self.job.write({'employee_ids': [(6, 0, [self.employee.id])]})

        source_location = self.job._get_source_location()
        self.assertEqual(source_location, self.vehicle.stock_location_id)

    def test_03_warehouse_location_fallback(self):
        """Test warehouse location is fallback"""
        # No team or employee assignment
        source_location = self.job._get_source_location()

        # Should be warehouse or internal location
        self.assertIn(source_location.usage, ['internal', 'view'])

    def test_04_team_overrides_technician(self):
        """Test team vehicle overrides technician vehicle"""
        # Create second vehicle for technician
        tech_vehicle = self.env['fleet.vehicle'].create({
            'name': 'Tech Vehicle',
            'license_plate': 'TECH-001',
            'model_id': self.vehicle_model.id,
        })
        self.employee.write({'vehicle_id': tech_vehicle.id})

        # Assign team with vehicle
        self.team.write({'vehicle_id': self.vehicle.id})
        # Create a new job with team and one of team members
        job_with_team = self.env['esfsm.job'].create({
            'name': 'JOB-TEAM-001',
            'partner_id': self.partner.id,
            'team_id': self.team.id,
            'scheduled_date_start': '2025-01-20 10:00:00',
        })

        source_location = job_with_team._get_source_location()
        # Should use team vehicle (higher priority)
        self.assertEqual(source_location, self.vehicle.stock_location_id)


@tagged('post_install', '-at_install', 'esfsm_stock')
class TestAddMaterialWizard(TestEsfsmStock):
    """Test Add Material Wizard"""

    def test_01_wizard_creation(self):
        """Test wizard can be created"""
        wizard = self.env['esfsm.add.material.wizard'].create({
            'job_id': self.job.id,
        })
        self.assertEqual(wizard.job_id, self.job)

    def test_02_wizard_adds_material(self):
        """Test wizard creates material line"""
        wizard = self.env['esfsm.add.material.wizard'].create({
            'job_id': self.job.id,
            'line_ids': [(0, 0, {
                'product_id': self.product.id,
                'product_uom_id': self.product.uom_id.id,
                'qty': 10.0,
            })],
        })

        initial_count = len(self.job.material_ids)
        wizard.action_confirm()

        self.assertEqual(len(self.job.material_ids), initial_count + 1)
        material = self.job.material_ids.filtered(lambda m: m.product_id == self.product)
        self.assertTrue(material)
        self.assertEqual(material.taken_qty, 10.0)

    def test_03_wizard_updates_existing_material(self):
        """Test wizard updates existing material line"""
        # Create initial material
        self._create_material(taken_qty=5.0)

        # Add more via wizard
        wizard = self.env['esfsm.add.material.wizard'].create({
            'job_id': self.job.id,
            'line_ids': [(0, 0, {
                'product_id': self.product.id,
                'product_uom_id': self.product.uom_id.id,
                'qty': 3.0,
            })],
        })

        wizard.action_confirm()

        material = self.job.material_ids.filtered(lambda m: m.product_id == self.product)
        self.assertEqual(len(material), 1)  # Should not create duplicate
        self.assertEqual(material.taken_qty, 8.0)  # 5.0 + 3.0

    def test_04_wizard_validates_empty_lines(self):
        """Test wizard validates no empty lines"""
        wizard = self.env['esfsm.add.material.wizard'].create({
            'job_id': self.job.id,
        })

        with self.assertRaises(ValidationError):
            wizard.action_confirm()

    def test_05_wizard_line_qty_constraint(self):
        """Test wizard line quantity constraint"""
        with self.assertRaises(ValidationError):
            self.env['esfsm.add.material.wizard'].create({
                'job_id': self.job.id,
                'line_ids': [(0, 0, {
                    'product_id': self.product.id,
                    'product_uom_id': self.product.uom_id.id,
                    'qty': -5.0,  # Negative quantity
                })],
            })


@tagged('post_install', '-at_install', 'esfsm_stock')
class TestReturnMaterialWizard(TestEsfsmStock):
    """Test Return Material Wizard"""

    def setUp(self):
        """Set up test material for return"""
        super().setUp()
        self.material = self._create_material(
            taken_qty=10.0,
            used_qty=6.0,
        )

    def test_01_wizard_auto_populates_lines(self):
        """Test wizard auto-populates returnable materials"""
        wizard = self.env['esfsm.return.material.wizard'].with_context(
            active_id=self.job.id
        ).create({
            'job_id': self.job.id,
        })

        self.assertEqual(len(wizard.line_ids), 1)
        line = wizard.line_ids[0]
        self.assertEqual(line.product_id, self.product)
        self.assertEqual(line.available_qty, 4.0)  # 10.0 - 6.0
        self.assertEqual(line.return_qty, 4.0)  # Default to full return

    def test_02_wizard_returns_material(self):
        """Test wizard updates returned_qty"""
        wizard = self.env['esfsm.return.material.wizard'].with_context(
            active_id=self.job.id
        ).create({
            'job_id': self.job.id,
        })

        self.assertEqual(self.material.returned_qty, 0.0)

        wizard.action_confirm()

        self.assertEqual(self.material.returned_qty, 4.0)
        self.assertEqual(self.material.available_to_return_qty, 0.0)

    def test_03_wizard_partial_return(self):
        """Test wizard allows partial return"""
        wizard = self.env['esfsm.return.material.wizard'].with_context(
            active_id=self.job.id
        ).create({
            'job_id': self.job.id,
        })

        # Update return quantity to partial
        wizard.line_ids[0].write({'return_qty': 2.0})
        wizard.action_confirm()

        self.assertEqual(self.material.returned_qty, 2.0)
        self.assertEqual(self.material.available_to_return_qty, 2.0)

    def test_04_wizard_validates_return_qty(self):
        """Test wizard validates return quantity"""
        wizard = self.env['esfsm.return.material.wizard'].with_context(
            active_id=self.job.id
        ).create({
            'job_id': self.job.id,
        })

        with self.assertRaises(ValidationError):
            wizard.line_ids[0].write({'return_qty': 10.0})  # Exceeds available

    def test_05_wizard_skips_fully_returned_materials(self):
        """Test wizard doesn't show fully returned materials"""
        # Return all material
        self.material.write({'returned_qty': 4.0})

        wizard = self.env['esfsm.return.material.wizard'].with_context(
            active_id=self.job.id
        ).create({
            'job_id': self.job.id,
        })

        self.assertEqual(len(wizard.line_ids), 0)

    def test_06_wizard_validates_empty_lines(self):
        """Test wizard validates no returnable materials"""
        # Use all material
        self.material.write({'used_qty': 10.0})

        wizard = self.env['esfsm.return.material.wizard'].with_context(
            active_id=self.job.id
        ).create({
            'job_id': self.job.id,
        })

        with self.assertRaises(ValidationError):
            wizard.action_confirm()


@tagged('post_install', '-at_install', 'esfsm_stock')
class TestJobActions(TestEsfsmStock):
    """Test job action methods"""

    def test_01_action_view_materials(self):
        """Test action_view_materials returns correct action"""
        action = self.job.action_view_materials()

        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['res_model'], 'esfsm.job.material')
        self.assertIn(action['view_mode'], ['list,form', 'tree,form'])  # Accept both Odoo 17 and 18 formats
        self.assertIn(('job_id', '=', self.job.id), action['domain'])

    def test_02_action_add_materials(self):
        """Test action_add_materials returns correct action"""
        action = self.job.action_add_materials()

        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['res_model'], 'esfsm.add.material.wizard')
        self.assertEqual(action['view_mode'], 'form')
        self.assertEqual(action['target'], 'new')

    def test_03_action_return_materials(self):
        """Test action_return_materials returns correct action"""
        action = self.job.action_return_materials()

        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['res_model'], 'esfsm.return.material.wizard')
        self.assertEqual(action['view_mode'], 'form')
        self.assertEqual(action['target'], 'new')
