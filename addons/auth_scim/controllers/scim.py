# Part of Odoo. See LICENSE file for full copyright and licensing details.

import json
import logging
import re
import uuid

from odoo import SUPERUSER_ID, http
from odoo.http import request

_logger = logging.getLogger(__name__)

SCIM_CONTENT_TYPE = 'application/scim+json; charset=utf-8'

# SCIM schemas
SCHEMA_USER = 'urn:ietf:params:scim:schemas:core:2.0:User'
SCHEMA_LIST = 'urn:ietf:params:scim:api:messages:2.0:ListResponse'
SCHEMA_PATCH = 'urn:ietf:params:scim:api:messages:2.0:PatchOp'
SCHEMA_ERROR = 'urn:ietf:params:scim:api:messages:2.0:Error'


class SCIMController(http.Controller):

    # ------------------------------------------------------------------
    # Auth helper
    # ------------------------------------------------------------------

    def _scim_authenticate(self):
        """Validate the SCIM bearer token from the Authorization header.
        Returns True or raises a werkzeug HTTP error.
        """
        header = request.httprequest.headers.get('Authorization', '')
        match = re.match(r'^bearer\s+(.+)$', header, re.IGNORECASE)
        if not match:
            return self._scim_error('Unauthorized', 401)
        token = match.group(1)
        if not request.env['auth.scim.token'].sudo()._validate_token(token):
            return self._scim_error('Invalid or inactive SCIM token', 401)
        return None

    def _scim_enabled(self):
        """Check if SCIM provisioning is enabled."""
        return request.env['ir.config_parameter'].sudo().get_param(
            'auth_scim.enabled', 'False'
        ).lower() in ('true', '1')

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    def _scim_response(self, data, status=200):
        return request.make_json_response(data, status=status, headers=[
            ('Content-Type', SCIM_CONTENT_TYPE),
        ])

    def _scim_error(self, detail, status=400):
        return request.make_json_response({
            'schemas': [SCHEMA_ERROR],
            'detail': detail,
            'status': str(status),
        }, status=status, headers=[
            ('Content-Type', SCIM_CONTENT_TYPE),
        ])

    # ------------------------------------------------------------------
    # User <-> SCIM mapping
    # ------------------------------------------------------------------

    def _user_to_scim(self, user):
        """Convert an Odoo res.users record to a SCIM User resource."""
        base_url = request.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        names = (user.name or '').split(' ', 1)
        given_name = names[0] if names else ''
        family_name = names[1] if len(names) > 1 else ''

        return {
            'schemas': [SCHEMA_USER],
            'id': str(user.id),
            'externalId': user.scim_external_id or '',
            'userName': user.login,
            'name': {
                'givenName': given_name,
                'familyName': family_name,
                'formatted': user.name or '',
            },
            'displayName': user.name or '',
            'emails': [{
                'value': user.email or user.login,
                'type': 'work',
                'primary': True,
            }],
            'active': user.active,
            'meta': {
                'resourceType': 'User',
                'created': user.create_date.isoformat() + 'Z' if user.create_date else '',
                'lastModified': user.write_date.isoformat() + 'Z' if user.write_date else '',
                'location': f'{base_url}/scim/v2/Users/{user.id}',
            },
        }

    def _scim_to_user_vals(self, data):
        """Extract Odoo user values from a SCIM User resource dict."""
        vals = {}

        if 'userName' in data:
            vals['login'] = data['userName']

        if 'externalId' in data:
            vals['scim_external_id'] = data['externalId']

        # Name handling — prefer structured name, fallback to displayName
        if 'name' in data:
            name_data = data['name']
            parts = [name_data.get('givenName', ''), name_data.get('familyName', '')]
            formatted = ' '.join(p for p in parts if p).strip()
            if formatted:
                vals['name'] = formatted
            elif name_data.get('formatted'):
                vals['name'] = name_data['formatted']
        elif 'displayName' in data:
            vals['name'] = data['displayName']

        # Email
        if 'emails' in data and data['emails']:
            primary = next((e for e in data['emails'] if e.get('primary')), data['emails'][0])
            vals['email'] = primary.get('value', '')

        # Active status
        if 'active' in data:
            # Entra sends "True"/"False" as strings sometimes
            active = data['active']
            if isinstance(active, str):
                vals['active'] = active.lower() == 'true'
            else:
                vals['active'] = bool(active)

        return vals

    # ------------------------------------------------------------------
    # SCIM Filter parsing (Entra-compatible subset)
    # ------------------------------------------------------------------

    def _parse_filter(self, filter_str):
        """Parse a SCIM filter string into an Odoo domain.

        Only supports the subset that Entra actually sends:
          - userName eq "value"
          - externalId eq "value"
          - displayName eq "value"
        """
        if not filter_str:
            return []

        FIELD_MAP = {
            'username': 'login',
            'externalid': 'scim_external_id',
            'displayname': 'name',
        }

        # Pattern: fieldName op "value"
        match = re.match(
            r'^\s*(\w+)\s+(eq|co|sw)\s+"([^"]*)"\s*$',
            filter_str,
            re.IGNORECASE,
        )
        if not match:
            _logger.warning('SCIM: unsupported filter expression: %s', filter_str)
            return []

        field_name = match.group(1).lower()
        operator = match.group(2).lower()
        value = match.group(3)

        odoo_field = FIELD_MAP.get(field_name)
        if not odoo_field:
            _logger.warning('SCIM: unknown filter field: %s', field_name)
            return []

        OP_MAP = {
            'eq': '=',
            'co': 'like',
            'sw': '=like',
        }
        odoo_op = OP_MAP[operator]

        if odoo_op == '=like':
            value = f'{value}%'

        return [(odoo_field, odoo_op, value)]

    # ------------------------------------------------------------------
    # SCIM PATCH operation parsing (Entra-compatible)
    # ------------------------------------------------------------------

    def _apply_patch_operations(self, user, operations):
        """Apply SCIM PATCH operations to a user record.

        Entra sends operations like:
          {"op": "Replace", "path": "active", "value": "False"}
          {"op": "Replace", "path": "displayName", "value": "New Name"}

        Note: Entra capitalizes op values ("Replace" not "replace").
        """
        vals = {}
        for op_data in operations:
            op = op_data.get('op', '').lower()
            path = op_data.get('path', '').lower()
            value = op_data.get('value')

            if op != 'replace':
                _logger.warning('SCIM PATCH: unsupported op %r, skipping', op_data.get('op'))
                continue

            if path == 'active':
                if isinstance(value, str):
                    vals['active'] = value.lower() == 'true'
                elif isinstance(value, list):
                    # Entra sometimes sends [{"value": "False"}]
                    vals['active'] = str(value[0].get('value', 'true')).lower() == 'true'
                else:
                    vals['active'] = bool(value)
            elif path == 'username':
                vals['login'] = value if isinstance(value, str) else str(value)
            elif path == 'displayname':
                vals['name'] = value if isinstance(value, str) else str(value)
            elif path in ('name.givenname', 'name.familyname'):
                # Rebuild full name from parts
                current_parts = (user.name or '').split(' ', 1)
                given = current_parts[0] if current_parts else ''
                family = current_parts[1] if len(current_parts) > 1 else ''
                if path == 'name.givenname':
                    given = value if isinstance(value, str) else str(value)
                else:
                    family = value if isinstance(value, str) else str(value)
                vals['name'] = f'{given} {family}'.strip()
            elif path.startswith('emails'):
                if isinstance(value, list) and value:
                    vals['email'] = value[0].get('value', '')
                elif isinstance(value, str):
                    vals['email'] = value
            elif path == 'externalid':
                vals['scim_external_id'] = value if isinstance(value, str) else str(value)
            else:
                _logger.info('SCIM PATCH: ignoring unknown path %r', path)

        if vals:
            user.sudo().write(vals)

    # ------------------------------------------------------------------
    # SCIM User endpoints
    # ------------------------------------------------------------------

    @http.route('/scim/v2/Users', type='http', auth='public', methods=['GET'],
                csrf=False, save_session=False)
    def scim_get_users(self, **kwargs):
        auth_error = self._scim_authenticate()
        if auth_error:
            return auth_error
        if not self._scim_enabled():
            return self._scim_error('SCIM provisioning is not enabled', 403)

        filter_str = kwargs.get('filter', '')
        start_index = int(kwargs.get('startIndex', 1))
        count = int(kwargs.get('count', 100))

        domain = self._parse_filter(filter_str)
        # Include inactive users — Entra expects to find deactivated users
        users = request.env['res.users'].sudo().with_context(active_test=False).search(
            domain, offset=max(start_index - 1, 0), limit=count,
        )
        total = request.env['res.users'].sudo().with_context(active_test=False).search_count(domain)

        return self._scim_response({
            'schemas': [SCHEMA_LIST],
            'totalResults': total,
            'startIndex': start_index,
            'itemsPerPage': count,
            'Resources': [self._user_to_scim(u) for u in users],
        })

    @http.route('/scim/v2/Users/<int:user_id>', type='http', auth='public',
                methods=['GET'], csrf=False, save_session=False)
    def scim_get_user(self, user_id, **kwargs):
        auth_error = self._scim_authenticate()
        if auth_error:
            return auth_error
        if not self._scim_enabled():
            return self._scim_error('SCIM provisioning is not enabled', 403)

        user = request.env['res.users'].sudo().with_context(active_test=False).browse(user_id)
        if not user.exists():
            return self._scim_error('User not found', 404)

        return self._scim_response(self._user_to_scim(user))

    @http.route('/scim/v2/Users', type='http', auth='public', methods=['POST'],
                csrf=False, save_session=False)
    def scim_create_user(self, **kwargs):
        auth_error = self._scim_authenticate()
        if auth_error:
            return auth_error
        if not self._scim_enabled():
            return self._scim_error('SCIM provisioning is not enabled', 403)

        try:
            data = json.loads(request.httprequest.get_data(as_text=True))
        except (json.JSONDecodeError, ValueError):
            return self._scim_error('Invalid JSON body', 400)

        vals = self._scim_to_user_vals(data)
        if not vals.get('login'):
            return self._scim_error('userName is required', 400)

        # Check for existing user with same login or externalId
        existing_domain = [('login', '=', vals['login'])]
        if vals.get('scim_external_id'):
            existing_domain = ['|', existing_domain[0],
                               ('scim_external_id', '=', vals['scim_external_id'])]
        existing = request.env['res.users'].sudo().with_context(active_test=False).search(
            existing_domain, limit=1,
        )
        if existing:
            # Entra expects 409 Conflict if user already exists
            return self._scim_error(
                f'User with userName "{vals["login"]}" already exists', 409,
            )

        # Set a random password — user will authenticate via SSO
        vals['password'] = uuid.uuid4().hex

        # auth='public' runs as public user. Switch to a proper admin env with
        # the main company pinned, otherwise computed fields on the new user
        # crash on empty/company-less envs.
        admin_env = request.env(user=SUPERUSER_ID)
        main_company = admin_env.ref('base.main_company', raise_if_not_found=False)
        if main_company and not vals.get('company_id'):
            vals['company_id'] = main_company.id
            vals['company_ids'] = [(6, 0, [main_company.id])]

        # Grant internal-user access so provisioned users land in the Odoo
        # backend after SSO instead of the empty "You are logged in" page.
        if 'group_ids' not in vals:
            internal_group = admin_env.ref('base.group_user', raise_if_not_found=False)
            if internal_group:
                vals['group_ids'] = [(4, internal_group.id)]

        Users = admin_env['res.users'].with_context(
            no_reset_password=True,
            mail_create_nosubscribe=True,
            mail_create_nolog=True,
            tracking_disable=True,
        )
        if main_company:
            Users = Users.with_company(main_company)

        try:
            user = Users.create(vals)
        except Exception as e:
            _logger.exception('SCIM: failed to create user')
            request.env.cr.rollback()
            return self._scim_error(f'Failed to create user: {e}', 500)

        return self._scim_response(self._user_to_scim(user), status=201)

    @http.route('/scim/v2/Users/<int:user_id>', type='http', auth='public',
                methods=['PATCH'], csrf=False, save_session=False)
    def scim_patch_user(self, user_id, **kwargs):
        auth_error = self._scim_authenticate()
        if auth_error:
            return auth_error
        if not self._scim_enabled():
            return self._scim_error('SCIM provisioning is not enabled', 403)

        user = request.env['res.users'].sudo().with_context(active_test=False).browse(user_id)
        if not user.exists():
            return self._scim_error('User not found', 404)

        try:
            data = json.loads(request.httprequest.get_data(as_text=True))
        except (json.JSONDecodeError, ValueError):
            return self._scim_error('Invalid JSON body', 400)

        operations = data.get('Operations', [])
        if not operations:
            return self._scim_error('No operations in PATCH request', 400)

        self._apply_patch_operations(user, operations)
        return self._scim_response(self._user_to_scim(user))

    @http.route('/scim/v2/Users/<int:user_id>', type='http', auth='public',
                methods=['PUT'], csrf=False, save_session=False)
    def scim_replace_user(self, user_id, **kwargs):
        """Full user replace — Entra rarely uses this, but some IdPs do."""
        auth_error = self._scim_authenticate()
        if auth_error:
            return auth_error
        if not self._scim_enabled():
            return self._scim_error('SCIM provisioning is not enabled', 403)

        user = request.env['res.users'].sudo().with_context(active_test=False).browse(user_id)
        if not user.exists():
            return self._scim_error('User not found', 404)

        try:
            data = json.loads(request.httprequest.get_data(as_text=True))
        except (json.JSONDecodeError, ValueError):
            return self._scim_error('Invalid JSON body', 400)

        vals = self._scim_to_user_vals(data)
        if vals:
            user.sudo().write(vals)

        return self._scim_response(self._user_to_scim(user))

    @http.route('/scim/v2/Users/<int:user_id>', type='http', auth='public',
                methods=['DELETE'], csrf=False, save_session=False)
    def scim_delete_user(self, user_id, **kwargs):
        auth_error = self._scim_authenticate()
        if auth_error:
            return auth_error
        if not self._scim_enabled():
            return self._scim_error('SCIM provisioning is not enabled', 403)

        user = request.env['res.users'].sudo().with_context(active_test=False).browse(user_id)
        if not user.exists():
            return self._scim_error('User not found', 404)

        # Deactivate instead of delete — safer and preserves audit trail
        user.sudo().write({'active': False})
        return request.make_response('', status=204, headers=[
            ('Content-Type', SCIM_CONTENT_TYPE),
        ])

    # ------------------------------------------------------------------
    # SCIM Discovery endpoints
    # ------------------------------------------------------------------

    @http.route('/scim/v2/ServiceProviderConfig', type='http', auth='public',
                methods=['GET'], csrf=False, save_session=False)
    def scim_service_provider_config(self, **kwargs):
        """Service Provider Configuration — required by Entra during setup test."""
        auth_error = self._scim_authenticate()
        if auth_error:
            return auth_error

        base_url = request.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        return self._scim_response({
            'schemas': ['urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig'],
            'documentationUri': f'{base_url}/scim/v2',
            'patch': {'supported': True},
            'bulk': {'supported': False, 'maxOperations': 0, 'maxPayloadSize': 0},
            'filter': {'supported': True, 'maxResults': 200},
            'changePassword': {'supported': False},
            'sort': {'supported': False},
            'etag': {'supported': False},
            'authenticationSchemes': [{
                'type': 'oauthbearertoken',
                'name': 'OAuth Bearer Token',
                'description': 'Authentication scheme using the OAuth Bearer Token standard',
            }],
        })

    @http.route('/scim/v2/Schemas', type='http', auth='public', methods=['GET'],
                csrf=False, save_session=False)
    def scim_schemas(self, **kwargs):
        """SCIM Schema discovery — Entra queries this during connection test."""
        auth_error = self._scim_authenticate()
        if auth_error:
            return auth_error

        user_schema = {
            'id': SCHEMA_USER,
            'name': 'User',
            'description': 'User Account',
            'attributes': [
                {
                    'name': 'userName',
                    'type': 'string',
                    'multiValued': False,
                    'required': True,
                    'caseExact': False,
                    'mutability': 'readWrite',
                    'returned': 'default',
                    'uniqueness': 'server',
                },
                {
                    'name': 'name',
                    'type': 'complex',
                    'multiValued': False,
                    'required': False,
                    'mutability': 'readWrite',
                    'returned': 'default',
                    'subAttributes': [
                        {'name': 'givenName', 'type': 'string', 'multiValued': False,
                         'required': False, 'mutability': 'readWrite', 'returned': 'default'},
                        {'name': 'familyName', 'type': 'string', 'multiValued': False,
                         'required': False, 'mutability': 'readWrite', 'returned': 'default'},
                        {'name': 'formatted', 'type': 'string', 'multiValued': False,
                         'required': False, 'mutability': 'readWrite', 'returned': 'default'},
                    ],
                },
                {
                    'name': 'displayName',
                    'type': 'string',
                    'multiValued': False,
                    'required': False,
                    'mutability': 'readWrite',
                    'returned': 'default',
                },
                {
                    'name': 'emails',
                    'type': 'complex',
                    'multiValued': True,
                    'required': False,
                    'mutability': 'readWrite',
                    'returned': 'default',
                    'subAttributes': [
                        {'name': 'value', 'type': 'string', 'multiValued': False,
                         'required': False, 'mutability': 'readWrite', 'returned': 'default'},
                        {'name': 'type', 'type': 'string', 'multiValued': False,
                         'required': False, 'mutability': 'readWrite', 'returned': 'default'},
                        {'name': 'primary', 'type': 'boolean', 'multiValued': False,
                         'required': False, 'mutability': 'readWrite', 'returned': 'default'},
                    ],
                },
                {
                    'name': 'active',
                    'type': 'boolean',
                    'multiValued': False,
                    'required': False,
                    'mutability': 'readWrite',
                    'returned': 'default',
                },
                {
                    'name': 'externalId',
                    'type': 'string',
                    'multiValued': False,
                    'required': False,
                    'caseExact': True,
                    'mutability': 'readWrite',
                    'returned': 'default',
                },
            ],
            'meta': {
                'resourceType': 'Schema',
                'location': '/scim/v2/Schemas/' + SCHEMA_USER,
            },
        }

        return self._scim_response({
            'schemas': [SCHEMA_LIST],
            'totalResults': 1,
            'Resources': [user_schema],
        })

    @http.route('/scim/v2/ResourceTypes', type='http', auth='public', methods=['GET'],
                csrf=False, save_session=False)
    def scim_resource_types(self, **kwargs):
        auth_error = self._scim_authenticate()
        if auth_error:
            return auth_error

        base_url = request.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        return self._scim_response({
            'schemas': [SCHEMA_LIST],
            'totalResults': 1,
            'Resources': [{
                'schemas': ['urn:ietf:params:scim:schemas:core:2.0:ResourceType'],
                'id': 'User',
                'name': 'User',
                'endpoint': '/scim/v2/Users',
                'description': 'User Account',
                'schema': SCHEMA_USER,
                'meta': {
                    'resourceType': 'ResourceType',
                    'location': f'{base_url}/scim/v2/ResourceTypes/User',
                },
            }],
        })
