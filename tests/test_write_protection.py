# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

"""Task 9 tests — harden material quantity writes (wizard / apply_*-only).

AUDIT (recorded here for provenance — this is the load-bearing finding):
    Every internal PRODUCTION code path that writes ``taken_qty`` /
    ``used_qty`` / ``returned_qty`` on ``esfsm.job.material`` passes
    ``skip_auto_picking=True`` in the write context:
      - esfsm_stock model apply_take / apply_consume / apply_return +
        _apply_consume_to_allocations / _apply_return_to_allocations
        → material_ctx(skip_auto_picking=True, skip_allocation_sum_check=True)
      - take / consume / return / add_material wizards
        → material.with_context(skip_auto_picking=True, ...)
      - esfsm_api controllers (add_material_usage, update_material_usage,
        return_materials) → route EXCLUSIVELY through
        apply_consume / apply_return (never a raw qty write); the sync
        allowlist in sync_queue.ALLOWED_SYNC_FIELDS explicitly EXCLUDES these
        three fields (only planned_qty is sync-writable).
      - migration / lot-resolution wizard write only flag / archive / lot_id
        scalars (never the three qty fields); their qty values land on the
        esfsm.job.material.lot allocation model, not the material.
    The PRODUCTION audit is therefore CLEAN.

DECISION (A only — view readonly; B NOT escalated):
    Escalating write() from a warning to a hard UserError was evaluated and
    found to be production-safe, BUT several PRE-EXISTING tests fabricate state
    with a raw ``material.write({'used_qty': ...})`` (or a write carrying only
    ``skip_allocation_sum_check`` — a DIFFERENT context flag, not
    ``skip_auto_picking``). A hard raise changed the suite's failure count from
    the pre-existing ~9. Per the conservative mandate the hard error was
    reverted to a warning; the enforced guards are the UI readonly fields
    (this test) plus the esfsm_api sync allowlist (Task 7/8). This test asserts
    the view hardening (A): the embedded per-lot allocation ``taken_qty`` is now
    readonly because it is engine-owned (it must equal the picking
    distribution).
"""

from lxml import etree

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install', 'esfsm_stock')
class TestWriteProtection(TransactionCase):

    def test_allocation_taken_qty_readonly_in_view(self):
        """The embedded per-lot allocation taken_qty must be readonly.

        It is engine-owned: it has to equal the validated picking's lot
        distribution, so a user must never type it directly in the allocations
        sub-list. The material-level taken/used/returned were already readonly;
        Task 9 closes the remaining editable allocation taken_qty.
        """
        view = self.env.ref('esfsm_stock.esfsm_job_material_view_form')
        arch = etree.fromstring(view.arch)

        # The allocation sub-list lives inside the lot_allocation_ids field node.
        alloc_field = arch.xpath("//field[@name='lot_allocation_ids']")
        self.assertTrue(alloc_field, 'lot_allocation_ids field must exist')

        nodes = alloc_field[0].xpath(".//field[@name='taken_qty']")
        self.assertTrue(
            nodes, 'embedded allocation taken_qty field must exist in the view')
        self.assertEqual(
            nodes[0].get('readonly'), '1',
            'embedded allocation taken_qty must be readonly (engine-owned: it '
            'must equal the validated picking distribution)')

    def test_material_scalar_qty_fields_readonly_in_views(self):
        """Regression guard: the three material-level qty scalars stay readonly
        in both the list and the form (the existing UI defence Task 9 relies on
        instead of a hard ORM error)."""
        form = self.env.ref('esfsm_stock.esfsm_job_material_view_form')
        list_view = self.env.ref('esfsm_stock.esfsm_job_material_view_list')

        for view in (form, list_view):
            arch = etree.fromstring(view.arch)
            for fname in ('taken_qty', 'used_qty', 'returned_qty'):
                # Material-level node (exclude the allocation sub-list nodes,
                # which live under lot_allocation_ids).
                nodes = [
                    n for n in arch.xpath("//field[@name='%s']" % fname)
                    if not any(
                        anc.get('name') == 'lot_allocation_ids'
                        for anc in n.iterancestors('field')
                    )
                ]
                self.assertTrue(
                    nodes, '%s must appear in %s' % (fname, view.name))
                self.assertEqual(
                    nodes[0].get('readonly'), '1',
                    '%s must be readonly in %s (UI is the enforced guard)'
                    % (fname, view.name))
