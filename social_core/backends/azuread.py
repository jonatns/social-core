import base64
import json
import time

from cryptography.x509 import load_pem_x509_certificate
from cryptography.hazmat.backends import default_backend
from jwt import DecodeError, ExpiredSignature, decode as jwt_decode

from ..exceptions import AuthTokenError
from .oauth import BaseOAuth2

"""
Copyright (c) 2015 Microsoft Open Technologies, Inc.

All rights reserved.

MIT License

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

"""
Azure AD OAuth2 backend, docs at:
    https://python-social-auth.readthedocs.io/en/latest/backends/azuread.html

See https://nicksnettravels.builttoroam.com/post/2017/01/24/Verifying-Azure-Active-Directory-JWT-Tokens.aspx
for verifying JWT tokens.
"""


class AzureADOAuth2(BaseOAuth2):
    name = 'azuread-oauth2'
    SCOPE_SEPARATOR = ' '
    OPENID_CONFIGURATION_URL = \
        'https://login.microsoftonline.com/{tenant_id}/.well-known/openid-configuration'
    AUTHORIZATION_URL = \
        'https://login.microsoftonline.com/{tenant_id}/oauth2/authorize'
    ACCESS_TOKEN_URL = 'https://login.microsoftonline.com/{tenant_id}/oauth2/token'
    JWKS_URL = 'https://login.microsoftonline.com/{tenant_id}/discovery/keys'
    ACCESS_TOKEN_METHOD = 'POST'
    REDIRECT_STATE = False
    DEFAULT_SCOPE = ['openid', 'profile', 'user_impersonation']
    EXTRA_DATA = [
        ('access_token', 'access_token'),
        ('id_token', 'id_token'),
        ('refresh_token', 'refresh_token'),
        ('expires_in', 'expires'),
        ('expires_on', 'expires_on'),
        ('not_before', 'not_before'),
        ('given_name', 'first_name'),
        ('family_name', 'last_name'),
        ('token_type', 'token_type')
    ]

    @property
    def tenant_id(self):
        return self.setting('TENANT_ID', 'common')

    def openid_configuration_url(self):
        return self.OPENID_CONFIGURATION_URL.format(tenant_id=self.tenant_id)

    def authorization_url(self):
        return self.AUTHORIZATION_URL.format(tenant_id=self.tenant_id)

    def access_token_url(self):
        return self.ACCESS_TOKEN_URL.format(tenant_id=self.tenant_id)

    def jwks_url(self):
        return self.JWKS_URL.format(tenant_id=self.tenant_id)

    def get_user_id(self, details, response):
        """Use upn as unique id"""
        return response.get('upn')

    def get_user_details(self, response):
        """Return user details from Azure AD account"""
        fullname, first_name, last_name = (
            response.get('name', ''),
            response.get('given_name', ''),
            response.get('family_name', '')
        )
        return {'username': fullname,
                'email': response.get('upn'),
                'fullname': fullname,
                'first_name': first_name,
                'last_name': last_name}

    def get_certificate(self, kid):
        # retrieve keys from jwks_url
        resp = self.request(self.jwks_url(), method="GET")
        resp.raise_for_status()

        # find the proper key for the kid
        for key in resp.json()["keys"]:
            if key['kid'] == kid:
                x5c = key['x5c'][0]
                break
        else:
            raise DecodeError("Cannot find kid={}".format(kid))

        certificate = "-----BEGIN CERTIFICATE-----\n" \
                      "{}\n" \
                      "-----END CERTIFICATE-----".format(x5c)

        return load_pem_x509_certificate(certificate.encode(),
                                         default_backend())

    def user_data(self, access_token, *args, **kwargs):
        response = kwargs.get('response')
        id_token = response.get('id_token')

        # decode the JWT header as JSON dict
        jwt_header = json.loads(
            base64.b64decode(id_token.split(".", 1)[0]).decode()
        )

        # get key id and algorithm
        key_id = jwt_header["kid"]
        algorithm = jwt_header["alg"]

        try:
            # retrieve certificate for key_id
            certificate = self.get_certificate(key_id)

            return jwt_decode(
                id_token,
                key=certificate.public_key(),
                algorithms=algorithm,
                audience=self.setting("SOCIAL_AUTH_AZUREAD_OAUTH2_KEY")
            )
        except (DecodeError, ExpiredSignature) as error:
            raise AuthTokenError(self, error)

    def auth_extra_arguments(self):
        """Return extra arguments needed on auth process. The defaults can be
        overriden by GET parameters."""
        extra_arguments = super(AzureADOAuth2, self).auth_extra_arguments()
        resource = self.setting('RESOURCE')
        if resource:
            extra_arguments.update({'resource': resource})
        return extra_arguments

    def extra_data(self, user, uid, response, details=None, *args, **kwargs):
        """Return access_token and extra defined names to store in
        extra_data field"""
        data = super(AzureADOAuth2, self).extra_data(user, uid, response,
                                                     details, *args, **kwargs)
        data['resource'] = self.setting('RESOURCE')
        return data

    def refresh_token_params(self, token, *args, **kwargs):
        return {
            'client_id': self.setting('KEY'),
            'client_secret': self.setting('SECRET'),
            'refresh_token': token,
            'grant_type': 'refresh_token',
            'resource': self.setting('RESOURCE')
        }

    def get_auth_token(self, user_id):
        """Return the access token for the given user, after ensuring that it
        has not expired, or refreshing it if so."""
        user = self.get_user(user_id=user_id)
        access_token = user.social_user.access_token
        expires_on = user.social_user.extra_data['expires_on']
        if expires_on <= int(time.time()):
            new_token_response = self.refresh_token(token=access_token)
            access_token = new_token_response['access_token']
        return access_token
