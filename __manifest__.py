# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

{
    'name': 'ESFSM - Stock & Fleet Integration',
    'version': '18.0.1.1.0',
    'category': 'Services/Field Service',
    'summary': 'Material tracking with fleet vehicle stock locations for field service jobs',
    'description': """
Field Service Management - Stock & Fleet Integration
=====================================================

This module extends ESFSM with comprehensive material tracking and fleet integration:

Key Features
------------
* **Automatic Vehicle Stock Locations**: Each vehicle automatically gets its own stock location
* **Team Vehicle Assignment**: Assign vehicles to field service teams
* **Material Lifecycle Tracking**: Track materials through planned → taken → used → returned
* **Smart Source Location**: Automatic priority (team vehicle > tech vehicle > warehouse)
* **Material Return Wizard**: Return unused materials from job to warehouse
* **Add Materials Wizard**: Request additional materials during job execution
* **Stock Picking Integration**: All material movements create traceable stock pickings

Material Lifecycle
------------------
1. **Planned** - Estimated materials needed for job
2. **Taken** - Materials picked from warehouse/vehicle to technician
3. **Used** - Materials consumed on job site
4. **Returned** - Unused materials returned to warehouse

Technical Details
-----------------
* Extends: esfsm.job, esfsm.team, fleet.vehicle, hr.employee, stock.picking
* New Models: esfsm.job.material, esfsm.return.material.wizard, esfsm.add.material.wizard
* Dependencies: esfsm, stock, fleet
* Multi-company compatible
* Full constraint validation for material quantities

    """,
    'author': 'ЕСКОН-ИНЖЕНЕРИНГ ДООЕЛ Струмица',
    'website': 'https://www.eskon.com.mk',
    'license': 'LGPL-3',
    'depends': [
        'esfsm',
        'stock',
        'fleet',
        'l10n_mk_reverse',
    ],
    'data': [
        # Security
        'security/ir.model.access.csv',

        # Data
        'data/stock_location_data.xml',

        # Views
        'views/esfsm_job_views.xml',
        'views/esfsm_job_material_views.xml',
        'views/esfsm_team_views.xml',
        'views/fleet_vehicle_views.xml',
        'views/hr_employee_views.xml',
        'views/stock_picking_views.xml',
        'views/wizard_views.xml',
    ],
    'demo': [],
    'installable': True,
    'application': False,
    'auto_install': False,
}
