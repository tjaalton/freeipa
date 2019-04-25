# Authors:
#   Petr Viktorin <pviktori@redhat.com>
#
# Copyright (C) 2013  Red Hat
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

"""Host class for integration testing"""
import subprocess

import pytest_multihost.host

from ipapython import ipaldap


class Host(pytest_multihost.host.Host):
    """Representation of a remote IPA host"""

    @staticmethod
    def _make_host(domain, hostname, role, ip, external_hostname):
        # We need to determine the type of the host, this depends on the domain
        # type, as we assume all Unix machines are in the Unix domain and
        # all Windows machine in a AD domain

        if domain.type == 'AD':
            cls = WinHost
        else:
            cls = Host

        return cls(domain, hostname, role, ip, external_hostname)

    def ldap_connect(self):
        """Return an LDAPClient authenticated to this host as directory manager
        """
        self.log.info('Connecting to LDAP at %s', self.external_hostname)
        ldap_uri = ipaldap.get_ldap_uri(self.external_hostname)
        ldap = ipaldap.LDAPClient(ldap_uri)
        binddn = self.config.dirman_dn
        self.log.info('LDAP bind as %s' % binddn)
        ldap.simple_bind(binddn, self.config.dirman_password)
        return ldap

    @classmethod
    def from_env(cls, env, domain, hostname, role, index, domain_index):
        from ipatests.pytest_ipa.integration.env_config import host_from_env
        return host_from_env(env, domain, hostname, role, index, domain_index)

    def to_env(self, **kwargs):
        from ipatests.pytest_ipa.integration.env_config import host_to_env
        return host_to_env(self, **kwargs)

    def run_command(self, argv, set_env=True, stdin_text=None,
                    log_stdout=True, raiseonerr=True,
                    cwd=None, bg=False, encoding='utf-8'):
        # Wrap run_command to log stderr on raiseonerr=True
        result = super(Host, self).run_command(
            argv, set_env=set_env, stdin_text=stdin_text,
            log_stdout=log_stdout, raiseonerr=False, cwd=cwd, bg=bg,
            encoding=encoding
        )
        if result.returncode and raiseonerr:
            result.log.error('stderr: %s', result.stderr_text)
            raise subprocess.CalledProcessError(
                result.returncode, argv,
                result.stderr_text
            )
        else:
            return result


class WinHost(pytest_multihost.host.WinHost):
    """
    Representation of a remote Windows host.

    This serves as a sketch class once we move from manual preparation of
    Active Directory to the automated setup.
    """
