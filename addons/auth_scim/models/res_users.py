# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging

from odoo import api, fields, models

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
        """Link SCIM-provisioned users to OAuth on first sign-in.

        Must run *before* super(): otherwise Odoo's default signup path may
        auto-create a fresh user with a synthetic `provider_N_user_X` login
        when the oauth_uid lookup misses, leaving the real SCIM user dangling
        and producing a duplicate account.
        """
        email = (
            validation.get('email')
            or validation.get('preferred_username')
            or validation.get('upn')
        )
        if email:
            scim_user = self.sudo().search([
                ('login', '=ilike', email),
                ('scim_external_id', '!=', False),
            ], limit=1)
            if scim_user:
                scim_user.write({
                    'oauth_provider_id': provider,
                    'oauth_uid': validation['user_id'],
                    'oauth_access_token': params['access_token'],
                })
                _logger.info(
                    'SCIM: linked OAuth identity to provisioned user %s',
                    scim_user.login,
                )
                return scim_user.login

        return super()._auth_oauth_signin(provider, validation, params)
