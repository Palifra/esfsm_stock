# ESFSM Stock - Unit Tests

## Test Coverage

This module includes comprehensive unit tests covering all major functionality:

### Test Suites (31 tests total)

1. **TestMaterialLifecycle** (7 tests)
   - Material line creation
   - Computed field: available_to_return_qty
   - Price subtotal computation
   - Used quantity constraint (cannot exceed taken)
   - Returned quantity constraint (cannot exceed available)
   - Material count on job
   - Material total on job

2. **TestFleetStockIntegration** (6 tests)
   - Vehicle auto-creates stock location
   - Vehicle location name updates with license plate
   - Vehicle location parent hierarchy
   - Team vehicle field assignment
   - Team stock location from vehicle
   - Employee vehicle field assignment

3. **TestLocationPriorityLogic** (4 tests)
   - Team vehicle location (priority 1)
   - Technician vehicle location (priority 2)
   - Warehouse location fallback (priority 3)
   - Team overrides technician priority

4. **TestAddMaterialWizard** (5 tests)
   - Wizard creation
   - Wizard adds new material
   - Wizard updates existing material (increments taken_qty)
   - Wizard validates empty lines
   - Wizard line quantity constraint (positive only)

5. **TestReturnMaterialWizard** (6 tests)
   - Wizard auto-populates returnable materials
   - Wizard returns materials and updates returned_qty
   - Wizard allows partial return
   - Wizard validates return quantity
   - Wizard skips fully returned materials
   - Wizard validates empty lines

6. **TestJobActions** (3 tests)
   - action_view_materials returns correct action
   - action_add_materials returns correct action
   - action_return_materials returns correct action

## Running Tests

### Via Odoo Shell
```bash
docker exec -i odoo_server odoo shell -d eskon --no-http << 'EOF'
import unittest
from odoo.addons.esfsm_stock.tests import test_esfsm_stock
suite = unittest.TestLoader().loadTestsFromModule(test_esfsm_stock)
unittest.TextTestRunner(verbosity=2).run(suite)
EOF
```

### Via Odoo CLI (requires server restart)
```bash
docker exec odoo_server python3 /usr/bin/odoo -d eskon \
    --test-enable --test-tags=esfsm_stock --stop-after-init --log-level=test
```

## Test Results

**All 31 tests passing** ✅

```
============================================================
TEST SUMMARY
============================================================
Tests run: 31
Successes: 31
Failures: 0
Errors: 0
============================================================
✓ ALL TESTS PASSED!
```

## Key Testing Patterns

### Helper Method
All test classes inherit from `TestEsfsmStock` which provides:
```python
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
```

### Test Data Setup
Common test data created in `setUpClass`:
- Partner (customer)
- Product (consumable)
- Fleet vehicle model and brand
- Vehicle with stock location
- Employee
- Team with members
- Job

### Constraint Testing
```python
with self.assertRaises(ValidationError):
    material.write({'used_qty': 10.0})  # Exceeds taken_qty
```

### Computed Field Testing
```python
material = self._create_material(taken_qty=10.0, used_qty=6.0)
self.assertEqual(material.available_to_return_qty, 4.0)
```

## Coverage Areas

✅ **Models**: esfsm.job, esfsm.job.material, fleet.vehicle, esfsm.team, hr.employee
✅ **Wizards**: esfsm.add.material.wizard, esfsm.return.material.wizard
✅ **Computed Fields**: All computed fields tested
✅ **Constraints**: All validation constraints tested
✅ **Actions**: All action methods tested
✅ **Integration**: Fleet-stock integration tested
✅ **Priority Logic**: Location priority tested

## Future Enhancements

- [ ] Test stock picking creation
- [ ] Test chatter messages
- [ ] Test with multiple companies
- [ ] Test access rules per user group
- [ ] Integration tests with full workflow

---

**Test Framework**: Odoo TransactionCase
**Tags**: `post_install`, `-at_install`, `esfsm_stock`
**Execution Time**: ~1 second
