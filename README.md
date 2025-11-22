# ESFSM - Stock & Fleet Integration

Comprehensive material tracking with fleet vehicle stock locations for Odoo 18 field service jobs.

## Features

### 🚗 Automatic Vehicle Stock Locations
- Each fleet vehicle automatically gets its own dedicated stock location
- Locations are organized under "Возила" (Vehicles) parent location
- Location names auto-update when vehicle license plate changes
- Full integration with Odoo's stock management system

### 📦 Material Lifecycle Tracking
Track materials through their complete lifecycle:
1. **Планирано** (Planned) - Estimated materials needed for the job
2. **Земено** (Taken) - Materials picked from warehouse/vehicle to technician
3. **Искористено** (Used) - Materials consumed on the job site
4. **Вратено** (Returned) - Unused materials returned to warehouse

### 🎯 Smart Location Priority
Automatic source location detection with priority:
1. **Team Vehicle** (highest priority)
2. **Technician Vehicle** (second priority)
3. **Warehouse** (fallback)

### 🔄 Material Wizards
- **Add Materials Wizard** - Request additional materials during job execution
- **Return Materials Wizard** - Return unused materials from job to warehouse
- Both wizards auto-populate based on job context and available quantities

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

### Adding Materials to a Job

#### Method 1: Direct Entry (Form View)
1. Open job form
2. Go to **Материјали** (Materials) tab
3. Add line with product, quantities
4. UoM and price auto-populate from product

#### Method 2: Add Materials Wizard
1. Click **Додади материјали** (Add Materials) button
2. Select products and quantities
3. Confirm → Materials added with `taken_qty` set

### Material Lifecycle Example

```python
# Job created with technician assigned
job = env['esfsm.job'].create({
    'partner_id': customer.id,
    'employee_id': technician.id,  # Has vehicle assigned
})

# Add material
material = env['esfsm.job.material'].create({
    'job_id': job.id,
    'product_id': cable_product.id,
    # UoM and price auto-filled!
    'planned_qty': 100.0,  # Estimated need
})

# During job preparation - material picked from warehouse
material.write({'taken_qty': 80.0})  # Actually took 80m

# After job completion - record usage
material.write({'used_qty': 65.0})  # Used 65m on site

# Return unused material
# available_to_return_qty = 15.0 (80 - 65)
material.write({'returned_qty': 15.0})  # Returned all unused
```

### Returning Materials

1. Click **Врати материјали** (Return Materials) button on job
2. Wizard pre-populates with returnable materials
3. Adjust quantities if needed (partial returns allowed)
4. Confirm → `returned_qty` updated, chatter message posted

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

### Wizards

#### `esfsm.add.material.wizard`
- Pre-fills job context
- Adds new materials or updates existing ones
- Creates stock picking (Warehouse → Vehicle)
- Posts chatter message

#### `esfsm.return.material.wizard`
- Auto-populates with returnable materials
- Supports partial returns
- Creates stock picking (Vehicle → Warehouse)
- Updates `returned_qty` on material lines

### Location Priority Logic

```python
def _get_source_location(self):
    """Priority: team vehicle > technician vehicle > warehouse"""
    if self.team_id and self.team_id.stock_location_id:
        return self.team_id.stock_location_id

    if self.employee_id and self.employee_id.vehicle_id.stock_location_id:
        return self.employee_id.vehicle_id.stock_location_id

    return warehouse.lot_stock_id  # Fallback
```

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
- `hr` (indirect via esfsm)

## Roadmap

- [ ] Stock picking generation (Phase C04 partial implementation)
- [ ] Integration with `stock.quant` for real-time inventory
- [ ] Barcode scanning for material picking
- [ ] Mobile app integration for material tracking
- [ ] Advanced reporting (material usage by job type, technician, etc.)

## Known Issues

None currently. Module is production-ready.

## Support

- **Email:** info@eskon.com.mk
- **Website:** https://www.eskon.com.mk
- **GitHub:** https://github.com/Palifra/esfsm_stock (coming soon)
- **Issues:** https://github.com/Palifra/esfsm_stock/issues

## Related Modules

- [esfsm](https://github.com/Palifra/esfsm) - Core Field Service Management
- [esfsm_project](https://github.com/Palifra/esfsm_project) - Project integration
- [esfsm_timesheet](https://github.com/Palifra/esfsm_timesheet) (planned)
- [esfsm_helpdesk](https://github.com/Palifra/esfsm_helpdesk) (planned)
- [esfsm_report](https://github.com/Palifra/esfsm_report) (planned)

## Credits

**Author:** ЕСКОН-ИНЖЕНЕРИНГ ДООЕЛ Струмица

**Contributors:**
- Filip Rajković <filip@eskon.com.mk>

**License:** LGPL-3

---

**Version:** 18.0.1.0.0
**Last Updated:** 2025-11-22
