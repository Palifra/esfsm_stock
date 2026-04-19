# ESFSM - Stock & Fleet Integration

Comprehensive material tracking with centralized location management for Odoo 18 field service jobs.

## Features

### 🏭 Centralized Location Provider (NEW in 18.0.1.2.0)
- Uses `eskon_reverse.stock.location.provider` for all stock locations
- Configurable location priority (vehicle/employee/team first)
- Settings UI for auto-creation options
- Consistent location hierarchy across all modules

### 🚗 Resource Stock Locations
- Stock locations for employees, vehicles, and teams
- Locations organized under centralized "Ресурси" hierarchy
- Automatic creation based on Settings configuration
- Full integration with Odoo's stock management system

### 📦 Material Lifecycle Tracking
Track materials through their complete lifecycle:
1. **Планирано** (Planned) - Estimated materials needed for the job
2. **Земено** (Taken) - Materials picked from warehouse/vehicle to technician
3. **Искористено** (Used) - Materials consumed on the job site
4. **Вратено** (Returned) - Unused materials returned to warehouse

### 🏷️ Per-Lot Allocation Tracking (NEW in 18.0.1.8.0)

Materials drawn from lot-tracked products (cables, batches) now maintain
per-lot accounting through the entire lifecycle — not just a single
"primary lot" pointer. Each material line can have N allocations, one per
(material × lot) pair, with independent taken/used/returned quantities.

**Features:**
- Sub-model `esfsm.job.material.lot` with `taken_qty`, `used_qty`,
  `returned_qty` per lot — automatically kept in sync with picking history.
- FEFO distribution for consume/return — consumes the lot with earliest
  expiration (or creation) date first.
- Feature flag `esfsm_stock.per_lot_allocations_enabled` for staged rollout.
- Daily drift detection cron that flags mismatches between material
  scalars and allocation sums.
- Phase 3 migration engine with dry-run, classifier, resolution wizard,
  and JSON-archive rollback.
- API v3 `lot_allocations[]` alongside legacy `lot_id` for backward
  compatibility with existing mobile clients.

**Consume/Return wizards now show one row per (material, lot)** — user
enters the consumed/returned quantity per lot directly. Historical gap
materials (pre-lot-tracking data) keep the legacy one-row-per-material UI.

See `docs/LOT_ALLOCATION.md` for architecture and `docs/MIGRATION_GUIDE.md`
for operator deployment steps.

### 🎯 Smart Location Priority
Automatic source location detection with priority:
1. **Team Vehicle** (highest priority)
2. **Technician Vehicle** (second priority)
3. **Warehouse** (fallback)

### 🔄 Strict Material Workflow (Wizard-Only)
Materials must follow a controlled 4-step workflow:

```
ДОДАДИ → ПРЕВЗЕМИ → ПОТРОШИ → ВРАТИ
(Add)     (Take)     (Consume)  (Return)
```

**Wizards:**
- **Add Materials Wizard** - Plan materials needed for the job
- **Take Materials Wizard** - Pick materials from warehouse (creates Реверс/Stock Issue)
- **Consume Materials Wizard** - Record actual usage (creates Испратница/Delivery)
- **Return Materials Wizard** - Return unused materials (creates Повратница/Return)

**Key constraints:**
- Quantity fields are readonly in views - changes only through wizards
- Job cannot be completed with unreturned materials
- All operations create proper stock documents for traceability

### 📊 Job Material Management
- Real-time material count and cost tracking on jobs
- Monetary subtotals with multi-currency support
- Smart buttons for quick access to materials
- Integrated filters for jobs with/without materials

## Installation

### Development Environment
```bash
# Clone the repository
git clone https://github.com/Palifra/esfsm_stock.git
cd esfsm_stock

# Install dependencies (esfsm, stock, fleet must be installed)
./odoo-bin -d your_db -i esfsm_stock --stop-after-init
```

### Docker Production Environment
```bash
# Fix permissions (required!)
chmod -R 755 /home/eskon/odoo/addons/esfsm_stock/
find /home/eskon/odoo/addons/esfsm_stock/ -type f -exec chmod 644 {} \;

# Install via shell
docker exec -i odoo_server odoo shell -d eskon --no-http << 'EOF'
env['ir.module.module'].update_list()
env.cr.commit()

module = env['ir.module.module'].search([('name', '=', 'esfsm_stock')])
module.button_immediate_install()
env.cr.commit()
print("Module installed!")
EOF

# Restart container
docker restart odoo_server
```

## Configuration

### 1. Create Vehicles
Navigate to **Fleet → Vehicles** and create vehicles. Stock locations are auto-created.

```python
# Example: Vehicle "Van-001" with license "SK-1234"
# Auto-creates location: "Возило - SK-1234"
```

### 2. Assign Vehicles to Teams/Employees
- **Teams**: Field Service → Configuration → Teams → Edit → Vehicle
- **Employees**: Employees → Edit → Vehicle

### 3. Set Up Products
Ensure materials have:
- Type: Storable (`type='consu'`) or Consumable
- Unit of Measure configured
- Standard price set

## Usage

### Complete Material Workflow

Materials must go through 4 stages (all via wizards):

#### Step 1: Add Materials (Planning)
1. Open job form → **Материјали** (Materials) tab
2. Click **[Додади]** button
3. Select products, lots (if tracked), and quantities
4. Click **[Потврди]**

**Result:** Materials added with `planned_qty` set

#### Step 2: Take Materials (From Warehouse)
1. Click **[Превземи]** button (visible when materials to take exist)
2. Wizard shows availability status:
   - 🟢 Green: Full stock available
   - 🟡 Yellow: Partial stock
   - 🔴 Red: No stock
3. Enter quantities to take
4. Click **[Превземи]**

**Result:**
- Stock document "Реверс" created (Warehouse → Vehicle)
- `taken_qty` updated
- Chatter message posted

#### Step 3: Consume Materials (At Customer Site)
1. Click **[Потроши]** button (visible when taken materials exist)
2. Enter actual consumed quantities
3. Click **[Потроши]**

**Result:**
- Stock document "Испратница" created (Vehicle → Customer)
- `used_qty` updated
- Material cost calculated

#### Step 4: Return Unused Materials
1. Click **[Врати]** button (visible when unreturned materials exist)
2. Wizard shows returnable quantities
3. Enter return quantities
4. Click **[Потврди]**

**Result:**
- Stock document "Повратница" created (Vehicle → Warehouse)
- `returned_qty` updated
- Inventory restored

### Job Completion Constraint

⚠️ **IMPORTANT:** A job cannot be completed if:
- There are taken materials that are neither consumed nor returned
- Formula must be satisfied: `taken_qty = used_qty + returned_qty`

### Stock Documents Created

| Document | When | From → To |
|----------|------|-----------|
| **Реверс** | Take | Warehouse → Vehicle |
| **Испратница** | Consume | Vehicle → Customer |
| **Повратница** | Return | Vehicle → Warehouse |

### Viewing Material Summary

```python
job.material_count  # Number of material lines
job.material_total  # Total cost (used_qty * price_unit summed)
```

Smart button on job form shows material count and opens filtered list.

## Technical Details

### Models

#### `esfsm.job.material`
**Fields:**
- `product_id` - Product/material
- `product_uom_id` - Unit of measure (auto-filled)
- `planned_qty` - Estimated quantity
- `taken_qty` - Picked quantity
- `used_qty` - Consumed quantity
- `returned_qty` - Returned quantity
- `available_to_return_qty` - Computed: `taken - used - returned`
- `price_unit` - Unit price (auto-filled)
- `price_subtotal` - Computed: `used_qty * price_unit`

**Constraints:**
- `used_qty ≤ taken_qty`
- `returned_qty ≤ (taken_qty - used_qty)`

#### Extensions
- `fleet.vehicle` - Added `stock_location_id` (auto-created on vehicle creation)
- `esfsm.team` - Added `vehicle_id` and related `stock_location_id`
- `hr.employee` - Added `vehicle_id`
- `esfsm.job` - Added `material_ids`, `material_count`, `material_total`, `currency_id`
- `stock.picking` - Added `esfsm_job_id` for traceability

#### Stock Picking Service
- `esfsm.stock.picking.service` - Centralized service for creating stock pickings
  - `create_reverse_picking()` - Creates Реверс (warehouse → vehicle)
  - `create_delivery_picking()` - Creates Испратница (vehicle → customer)
  - `create_return_picking()` - Creates Повратница (vehicle → warehouse)
  - Handles lot tracking, technician name resolution, and picking type fallbacks

### Wizards

#### `esfsm.add.material.wizard`
- Pre-fills job context
- Allows selecting products and lots
- Creates material lines with `planned_qty`
- No stock movement (planning only)

#### `esfsm.take.material.wizard`
- Auto-populates with materials to take (planned_qty > taken_qty)
- Shows real-time stock availability with color coding
- Creates "Реверс" picking (Warehouse → Vehicle)
- Updates `taken_qty` on material lines
- Supports lot tracking

#### `esfsm.consume.material.wizard`
- Auto-populates with taken materials
- Shows available to consume (taken - used - returned)
- Creates "Испратница" picking (Vehicle → Customer)
- Updates `used_qty` on material lines
- Calculates material cost

#### `esfsm.return.material.wizard`
- Auto-populates with returnable materials
- Shows available to return (taken - used - returned)
- Creates "Повратница" picking (Vehicle → Warehouse)
- Updates `returned_qty` on material lines
- Supports partial returns

### Location Priority Logic (via eskon_reverse)

The module uses the centralized Location Provider from `eskon_reverse`:

```python
def _get_source_location(self):
    """
    Uses eskon_reverse.stock.location.provider for location resolution.
    Priority is configurable via Settings:
    - 'vehicle': Vehicle → Employee → Warehouse
    - 'employee': Employee → Vehicle → Warehouse
    - 'team': Team → Vehicle → Employee → Warehouse
    """
    provider = self.env['stock.location.provider']
    location = provider.get_fsm_location(self)

    if location:
        return location

    # Fallback to warehouse
    return warehouse.lot_stock_id
```

Configure priority in **Inventory → Configuration → Settings → Location Provider**.

## Testing

Run comprehensive test suite (30+ test cases):

```bash
# Run all esfsm_stock tests
docker exec odoo_server odoo -d eskon -u esfsm_stock --test-enable --test-tags=/esfsm_stock --stop-after-init --workers=0

# Test categories:
# - Material lifecycle (7 tests)
# - Fleet-stock integration (6 tests)
# - Location priority logic (4 tests)
# - Add material wizard (5 tests)
# - Return material wizard (6 tests)
# - Job actions (3 tests)
```

## Security

### Groups
Inherits from `esfsm` module:
- **FSM User** (`esfsm.group_esfsm_user`) - CRUD on materials, wizards
- **FSM Manager** (`esfsm.group_esfsm_manager`) - Full access including delete

### Access Control
- Material lines: User (CRUD), Manager (CRUD + Delete)
- Wizards: Both groups have full access (transient models)

## Dependencies

- `esfsm` - Core field service management
- `stock` - Inventory management
- `fleet` - Vehicle management
- `eskon_reverse` - Centralized Location Provider (NEW)
- `hr` (indirect via esfsm)

## Roadmap

- [x] ~~Stock picking generation~~ ✅ Implemented (Реверс, Испратница, Повратница)
- [x] ~~Wizard-only quantity control~~ ✅ Implemented
- [x] ~~Job completion blocking for unreturned materials~~ ✅ Implemented
- [ ] Barcode scanning for material picking
- [ ] Mobile app integration for material tracking
- [ ] Advanced reporting (material usage by job type, technician, etc.)
- [ ] Integration with purchase orders for material requests

## Known Issues

None currently. Module is production-ready with full test coverage.

## Support

- **Email:** info@eskon.com.mk
- **Website:** https://www.eskon.com.mk
- **GitHub:** https://github.com/Palifra/esfsm_stock (coming soon)
- **Issues:** https://github.com/Palifra/esfsm_stock/issues

## Related Modules

- [esfsm](https://github.com/Palifra/esfsm) - Core Field Service Management
- [eskon_reverse](https://github.com/Palifra/eskon_reverse) - Equipment borrowing & Location Provider
- [esfsm_project](https://github.com/Palifra/esfsm_project) - Project integration
- [l10n_mk_stock_reports](https://github.com/Palifra/l10n_mk_stock_reports) - Stock reports (depends on eskon_reverse)
- [esfsm_timesheet](https://github.com/Palifra/esfsm_timesheet) (planned)
- [esfsm_helpdesk](https://github.com/Palifra/esfsm_helpdesk) (planned)

## Credits

**Author:** ЕСКОН-ИНЖЕНЕРИНГ ДООЕЛ Струмица

**Contributors:**
- Filip Rajković <filip@eskon.com.mk>

**License:** LGPL-3

---

## Changelog

### 18.0.1.2.0 (2024-12-07)
- **BREAKING:** Changed dependency from `l10n_mk_reverse` to `eskon_reverse`
- Integrated with centralized Location Provider service
- Uses `stock.location.provider.get_fsm_location()` for location resolution
- Location hierarchy now managed by eskon_reverse module
- Removed duplicate location creation logic

### 18.0.1.1.0 (2024-12-04)
- Full wizard-based material workflow
- Stock picking generation for all operations
- Job completion blocking for unreturned materials

### 18.0.1.0.0
- Initial release with material tracking
- Vehicle stock locations

---

**Version:** 18.0.1.2.0
**Last Updated:** 2024-12-07
