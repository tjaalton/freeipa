#
# Copyright (C) 2015  FreeIPA Contributors see COPYING for license
#

from ipalib import errors
from ipalib import Registry
from ipalib import Updater
from ipapython.dn import DN
from ipaserver.install import certs, cainstance
from ipaserver.install import ldapupdate
from ipaplatform.paths import paths

register = Registry()


@register()
class update_ca_topology(Updater):
    """
    Updates CA topology configuration entries
    """

    def execute(self, **options):

        ca = cainstance.CAInstance(self.api.env.realm, certs.NSS_DIR)
        if not ca.is_configured():
            self.log.debug("CA is not configured on this host")
            return False, []

        ld = ldapupdate.LDAPUpdate(ldapi=True, sub_dict={
            'SUFFIX': self.api.env.basedn,
            'FQDN': self.api.env.host,
        })

        ld.update([paths.CA_TOPOLOGY_ULDIF])

        ldap = self.api.Backend.ldap2

        ca_replica_dn = DN(
            ('cn', 'replica'),
            ('cn', 'o=ipaca'),
            ('cn', 'mapping tree'),
            ('cn', 'config'))

        check_interval_attr = 'nsds5replicabinddngroupcheckinterval'
        default_check_interval = ['60']

        try:
            ca_replica_entry = ldap.get_entry(ca_replica_dn)
        except errors.NotFound:
            pass
        else:
            if check_interval_attr not in ca_replica_entry:
                ca_replica_entry[check_interval_attr] = default_check_interval
                ldap.update_entry(ca_replica_entry)

        return False, []
