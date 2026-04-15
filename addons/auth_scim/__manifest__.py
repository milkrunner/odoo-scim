# Part of Odoo. See LICENSE file for full copyright and licensing details.

{
    'name': 'SCIM 2.0 Provisioning',
    'version': '19.0.1.0.0',
    'category': 'Hidden/Tools',
    'description': """
SCIM 2.0 User Provisioning for Microsoft Entra ID
===================================================

Provides SCIM 2.0 endpoints for automated user provisioning
from Microsoft Entra ID (Azure AD).

Supported operations:
- User discovery (GET /scim/v2/Users)
- User creation (POST /scim/v2/Users)
- User update (PATCH /scim/v2/Users/:id)
- User deactivation (DELETE /scim/v2/Users/:id)
- Schema discovery (GET /scim/v2/Schemas, /ServiceProviderConfig)
""",
    'depends': ['base', 'web', 'base_setup'],
    'data': [
        'security/ir.model.access.csv',
        'views/res_config_settings_views.xml',
    ],
    'author': 'Odoo Community',
    'license': 'LGPL-3',
    'installable': True,
    'auto_install': False,
}
