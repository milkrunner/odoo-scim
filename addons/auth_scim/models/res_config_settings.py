# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    scim_enabled = fields.Boolean(
        string='Enable SCIM Provisioning',
        config_parameter='auth_scim.enabled',
    )
    scim_base_url = fields.Char(
        string='SCIM Tenant URL',
        compute='_compute_scim_base_url',
    )

    @api.depends('scim_enabled')
    def _compute_scim_base_url(self):
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        for record in self:
            record.scim_base_url = f"{base_url}/scim/v2"
