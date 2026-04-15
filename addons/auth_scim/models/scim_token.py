# Part of Odoo. See LICENSE file for full copyright and licensing details.

import secrets

from odoo import api, fields, models


class ScimToken(models.Model):
    _name = 'auth.scim.token'
    _description = 'SCIM Bearer Token'

    name = fields.Char(required=True, default='Default')
    token = fields.Char(
        required=True,
        default=lambda self: secrets.token_urlsafe(48),
        groups='base.group_system',
    )
    active = fields.Boolean(default=True)
    create_date = fields.Datetime(readonly=True)

    @api.model
    def _validate_token(self, token):
        """Validate a SCIM bearer token. Returns True if valid."""
        if not token:
            return False
        return bool(self.sudo().search([
            ('token', '=', token),
            ('active', '=', True),
        ], limit=1))

    def action_regenerate_token(self):
        self.ensure_one()
        self.token = secrets.token_urlsafe(48)
