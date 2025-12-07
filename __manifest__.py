# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

{
    'name': 'ESFSM - Stock & Fleet Integration',
    'version': '18.0.1.2.0',
    'category': 'Services/Field Service',
    'summary': 'Material tracking for field service jobs with centralized location management',
    'description': """
Field Service Management - Stock & Fleet Integration
=====================================================

This module extends ESFSM with comprehensive material tracking:

Key Features
------------
* **Centralized Location Provider**: Uses eskon_reverse for stock location management
* **Team Vehicle Assignment**: Assign vehicles to field service teams
* **Material Lifecycle Tracking**: Track materials through planned → taken → used → returned
* **Configurable Location Priority**: Settings control priority (vehicle/employee/team first)
* **Strict Wizard-Only Workflow**: Quantity fields are readonly - all changes via wizards
* **Add Materials Wizard**: Plan materials needed for the job
* **Take Materials Wizard**: Pick materials from warehouse (creates Реверс document)
* **Consume Materials Wizard**: Record actual usage (creates Испратница document)
* **Return Materials Wizard**: Return unused materials (creates Повратница document)
* **Stock Picking Integration**: All material movements create traceable stock pickings
* **Job Completion Control**: Job cannot complete with unreturned materials

Material Lifecycle
------------------
1. **Planned** - Estimated materials needed for job
2. **Taken** - Materials picked from warehouse/vehicle to technician
3. **Used** - Materials consumed on job site
4. **Returned** - Unused materials returned to warehouse

Location Management
-------------------
Stock locations for resources (employees, vehicles) are managed centrally by eskon_reverse.
This module uses the stock.location.provider service to determine:
- Source locations for material picking
- Destination locations for returns
- FSM job location priority based on Settings configuration

Technical Details
-----------------
* Extends: esfsm.job, esfsm.team, hr.employee, stock.picking
* New Models: esfsm.job.material, esfsm.add.material.wizard, esfsm.take.material.wizard, esfsm.consume.material.wizard, esfsm.return.material.wizard
* Dependencies: esfsm, stock, fleet, eskon_reverse
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
        'eskon_reverse',
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
