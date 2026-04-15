# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import fields, models


class ResUsers(models.Model):
    _inherit = 'res.users'

    scim_external_id = fields.Char(
        string='SCIM External ID',
        copy=False,
        help='External identifier assigned by the SCIM client (e.g. Microsoft Entra ID).',
    )

    _uniq_scim_external_id = models.Constraint(
        'unique(scim_external_id)',
        'SCIM External ID must be unique.',
    )
