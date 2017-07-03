# Authors:
#   Alexander Bokovoy <abokovoy@redhat.com>
#
# Copyright (C) 2012  Red Hat
# see file 'COPYING' for use and warranty information
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from ipalib.install import certstore
from ipaplatform.paths import paths
from ipaserver.install import certs
from ipalib import Registry, errors
from ipalib import Updater
from ipapython import certdb
from ipapython.dn import DN

register = Registry()


@register()
class update_upload_cacrt(Updater):
    """
    Upload public CA certificate to LDAP
    """

    def execute(self, **options):
        db = certs.CertDB(self.api.env.realm, paths.HTTPD_ALIAS_DIR)
        ca_cert = None

        ca_enabled = self.api.Command.ca_is_enabled()['result']
        if ca_enabled:
            ca_nickname = certdb.get_ca_nickname(self.api.env.realm)
        else:
            ca_nickname = None
            server_certs = db.find_server_certs()
            if server_certs:
                ca_chain = db.find_root_cert(server_certs[0][0])[:-1]
                if ca_chain:
                    ca_nickname = ca_chain[-1]

        ldap = self.api.Backend.ldap2

        for nickname, trust_flags in db.list_certs():
            if trust_flags.has_key:
                continue
            if nickname == ca_nickname and ca_enabled:
                trust_flags = certdb.IPA_CA_TRUST_FLAGS
            cert = db.get_cert_from_db(nickname, pem=False)
            trust, _ca, eku = certstore.trust_flags_to_key_policy(trust_flags)

            dn = DN(('cn', nickname), ('cn', 'certificates'), ('cn', 'ipa'),
                    ('cn','etc'), self.api.env.basedn)
            entry = ldap.make_entry(dn)

            try:
                certstore.init_ca_entry(entry, cert, nickname, trust, eku)
            except Exception as e:
                self.log.warning("Failed to create entry for %s: %s",
                                 nickname, e)
                continue
            if nickname == ca_nickname:
                ca_cert = cert
                config = entry.setdefault('ipaConfigString', [])
                if ca_enabled:
                    config.append('ipaCa')
                config.append('ipaCa')

            try:
                ldap.add_entry(entry)
            except errors.DuplicateEntry:
                if nickname == ca_nickname and ca_enabled:
                    try:
                        ldap.update_entry(entry)
                    except errors.EmptyModlist:
                        pass

        if ca_cert:
            dn = DN(('cn', 'CACert'), ('cn', 'ipa'), ('cn','etc'),
                    self.api.env.basedn)
            try:
                entry = ldap.get_entry(dn)
            except errors.NotFound:
                entry = ldap.make_entry(dn)
                entry['objectclass'] = ['nsContainer', 'pkiCA']
                entry.single_value['cn'] = 'CAcert'
                entry.single_value['cACertificate;binary'] = ca_cert
                ldap.add_entry(entry)
            else:
                if b'' in entry['cACertificate;binary']:
                    entry.single_value['cACertificate;binary'] = ca_cert
                    ldap.update_entry(entry)

        return False, []
