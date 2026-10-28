# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

"""Магационери (stock.group_stock_manager) гледаат СИТЕ материјали по налог,
само за читање — без да добијат пристап до самите налози (2026-10-28, Маја).

Правилата за записи се ИЛИ по групи: новото read-only правило го шири што
магационерот ГЛЕДА, а write/create/unlink остануваат под постоечкото
„FSM Material: User Access“ (само свои/доделени налози).
"""

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install', 'esfsm_stock')
class TestMaterialWarehouseReadonly(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)
        fsm_user = cls.env.ref('esfsm.group_esfsm_user')
        stock_mgr = cls.env.ref('stock.group_stock_manager')
        internal = cls.env.ref('base.group_user')

        cls.technician_user = Users.create({
            'name': 'Техничар Тест', 'login': 'esfsm_stock_tech_ro',
            'groups_id': [(6, 0, [internal.id, fsm_user.id])],
        })
        cls.technician = cls.env['hr.employee'].create({
            'name': 'Техничар Тест', 'user_id': cls.technician_user.id,
        })
        # Маја-профил: FSM User + Магацин/Администратор, не е доделена на налогот
        cls.warehouse_fsm_user = Users.create({
            'name': 'Магацин ФСМ', 'login': 'esfsm_stock_wh_fsm_ro',
            'groups_id': [(6, 0, [internal.id, fsm_user.id, stock_mgr.id])],
        })
        # чист магационер без FSM група
        cls.warehouse_only_user = Users.create({
            'name': 'Магацин Само', 'login': 'esfsm_stock_wh_only_ro',
            'groups_id': [(6, 0, [internal.id, stock_mgr.id])],
        })
        # обичен FSM User, не е доделен — не смее да види ништо (регресија)
        cls.other_fsm_user = Users.create({
            'name': 'Друг ФСМ', 'login': 'esfsm_stock_other_fsm_ro',
            'groups_id': [(6, 0, [internal.id, fsm_user.id])],
        })

        cls.partner = cls.env['res.partner'].create({'name': 'Клиент Тест РО'})
        cls.product = cls.env['product.product'].create({
            'name': 'Кабел тест РО', 'type': 'consu', 'is_storable': True,
            'standard_price': 10.0,
        })
        cls.job = cls.env['esfsm.job'].create({
            'name': 'RO-JOB-001',
            'partner_id': cls.partner.id,
            'employee_ids': [(4, cls.technician.id)],
            'scheduled_date_start': '2026-10-28 08:00:00',
        })
        cls.material = cls.env['esfsm.job.material'].create({
            'job_id': cls.job.id,
            'product_id': cls.product.id,
            'product_uom_id': cls.product.uom_id.id,
            'planned_qty': 5.0,
            'price_unit': 10.0,
        })

    def _materials(self, user):
        return self.env['esfsm.job.material'].with_user(user).search(
            [('product_id', '=', self.product.id)])

    def test_warehouse_fsm_user_reads_unassigned_job_material(self):
        found = self._materials(self.warehouse_fsm_user)
        self.assertEqual(found, self.material)
        values = found.read(['job_id', 'job_partner_id', 'planned_qty'])[0]
        # many2one display и related полето одат со sudo → името се гледа без пристап до налогот
        self.assertEqual(values['job_id'][1], self.job.display_name)
        self.assertEqual(values['job_partner_id'][0], self.partner.id)
        self.assertEqual(values['planned_qty'], 5.0)

    def test_open_jobs_filter_and_job_search_work_without_job_access(self):
        M = self.env['esfsm.job.material'].with_user(self.warehouse_fsm_user)
        # домен низ job_id би бил филтриран од правилата за налози → 0 резултати
        self.assertFalse(M.search([('job_id.stage_id.is_closed', '=', False),
                                   ('product_id', '=', self.product.id)]))
        # stored копиите на ставката се пребаруваат директно
        self.assertEqual(M.search([('job_is_closed', '=', False),
                                   ('product_id', '=', self.product.id)]), self.material)
        self.assertEqual(M.search([('job_name', 'ilike', 'RO-JOB-001')]), self.material)
        # промена на фаза на налогот ја ажурира копијата
        closed = self.env['esfsm.job.stage'].create({'name': 'Затворено РО', 'is_closed': True})
        self.job.stage_id = closed
        self.assertTrue(self.material.job_is_closed)
        self.assertFalse(M.search([('job_is_closed', '=', False),
                                   ('product_id', '=', self.product.id)]))

    def test_warehouse_fsm_user_cannot_see_the_job_itself(self):
        jobs = self.env['esfsm.job'].with_user(self.warehouse_fsm_user).search(
            [('id', '=', self.job.id)])
        self.assertFalse(jobs, 'магационерот не добива пристап до налозите, само до материјалите')

    def test_warehouse_user_cannot_write_create_or_unlink(self):
        material = self.material.with_user(self.warehouse_fsm_user)
        with self.assertRaises(AccessError):
            material.write({'planned_qty': 7.0})
        with self.assertRaises(AccessError):
            material.unlink()
        with self.assertRaises(AccessError):
            self.env['esfsm.job.material'].with_user(self.warehouse_fsm_user).create({
                'job_id': self.job.id,
                'product_id': self.product.id,
                'product_uom_id': self.product.uom_id.id,
                'planned_qty': 1.0,
            })
        self.assertEqual(self.material.planned_qty, 5.0)

    def test_warehouse_only_user_reads_via_new_acl(self):
        found = self._materials(self.warehouse_only_user)
        self.assertEqual(found, self.material)
        with self.assertRaises(AccessError):
            found.write({'planned_qty': 9.0})

    def test_plain_fsm_user_still_restricted(self):
        self.assertFalse(self._materials(self.other_fsm_user))
        # доделениот техничар си ги гледа своите, како и досега
        self.assertEqual(self._materials(self.technician_user), self.material)

    def test_warehouse_action_is_list_only_and_filtered(self):
        action = self.env.ref('esfsm_stock.esfsm_job_material_action_warehouse')
        self.assertEqual(action.view_mode, 'list')
        self.assertEqual(action.view_id,
                         self.env.ref('esfsm_stock.esfsm_job_material_view_list_warehouse'))
        self.assertIn('search_default_open_jobs', action.context)
        menu = self.env.ref('esfsm_stock.menu_esfsm_job_material_warehouse')
        self.assertEqual(menu.parent_id, self.env.ref('stock.menu_warehouse_report'))
        self.assertIn(self.env.ref('stock.group_stock_manager'), menu.groups_id)
