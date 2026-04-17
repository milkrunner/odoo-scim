# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging

from odoo import api, fields, models
from odoo.exceptions import AccessDenied

_logger = logging.getLogger(__name__)


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

    @api.model
    def _auth_oauth_signin(self, provider, validation, params):
        """Allow SCIM-provisioned users to sign in via OAuth on first contact.

        Odoo's default flow matches users by (oauth_provider_id, oauth_uid).
        For users created by SCIM those fields are empty, so the first OAuth
        login fails. When that happens, fall back to matching by email/login
        on the SCIM-provisioned pool and populate oauth_uid/oauth_provider_id
        so subsequent logins take the fast path.
        """
        try:
            return super()._auth_oauth_signin(provider, validation, params)
        except AccessDenied:
            login = (
                validation.get('email')
                or validation.get('preferred_username')
                or validation.get('upn')
            )
            if not login:
                raise

            user = self.sudo().search([
                ('login', '=', login),
                ('scim_external_id', '!=', False),
            ], limit=1)
            if not user:
                raise

            user.write({
                'oauth_provider_id': provider,
                'oauth_uid': validation['user_id'],
                'oauth_access_token': params['access_token'],
            })
            _logger.info(
                'SCIM: linked OAuth identity to SCIM-provisioned user %s', user.login,
            )
            return user.login
