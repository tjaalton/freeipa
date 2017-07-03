# Authors: Simo Sorce <ssorce@redhat.com>
#          Alexander Bokovoy <abokovoy@redhat.com>
#          Martin Kosek <mkosek@redhat.com>
#          Tomas Babej <tbabej@redhat.com>
#
# Copyright (C) 2007-2014  Red Hat
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

'''
This module contains default Red Hat OS family-specific implementations of
system tasks.
'''
from __future__ import print_function

import os
import pwd
import shutil
import socket
import base64
import traceback
import errno

from ctypes.util import find_library
from functools import total_ordering
from subprocess import CalledProcessError

from cffi import FFI
from pyasn1.error import PyAsn1Error
from six.moves import urllib

from ipapython.ipa_log_manager import root_logger, log_mgr
from ipapython import ipautil
import ipapython.errors

from ipaplatform.constants import constants
from ipaplatform.paths import paths
from ipaplatform.redhat.authconfig import RedHatAuthConfig
from ipaplatform.base.tasks import BaseTaskNamespace

# pylint: disable=ipa-forbidden-import
from ipalib.constants import IPAAPI_USER
# pylint: enable=ipa-forbidden-import

_ffi = FFI()
_ffi.cdef("""
int rpmvercmp (const char *a, const char *b);
""")

# use ctypes loader to get correct librpm.so library version according to
# https://cffi.readthedocs.org/en/latest/overview.html#id8
_librpm = _ffi.dlopen(find_library("rpm"))

log = log_mgr.get_logger(__name__)


def selinux_enabled():
    """
    Check if SELinux is enabled.
    """
    if os.path.exists(paths.SELINUXENABLED):
        try:
            ipautil.run([paths.SELINUXENABLED])
            return True
        except ipautil.CalledProcessError:
            # selinuxenabled returns 1 if not enabled
            return False
    else:
        # No selinuxenabled, no SELinux
        return False


@total_ordering
class IPAVersion(object):

    def __init__(self, version):
        self._version = version
        self._bytes = version.encode('utf-8')

    @property
    def version(self):
        return self._version

    def __eq__(self, other):
        if not isinstance(other, IPAVersion):
            return NotImplemented
        return _librpm.rpmvercmp(self._bytes, other._bytes) == 0

    def __lt__(self, other):
        if not isinstance(other, IPAVersion):
            return NotImplemented
        return _librpm.rpmvercmp(self._bytes, other._bytes) < 0

    def __hash__(self):
        return hash(self._version)


class RedHatTaskNamespace(BaseTaskNamespace):

    def restore_context(self, filepath, restorecon=paths.SBIN_RESTORECON):
        """
        restore security context on the file path
        SELinux equivalent is /path/to/restorecon <filepath>
        restorecon's return values are not reliable so we have to
        ignore them (BZ #739604).

        ipautil.run() will do the logging.
        """

        if not selinux_enabled():
            return

        if (os.path.exists(restorecon)):
            ipautil.run([restorecon, filepath], raiseonerr=False)

    def check_selinux_status(self, restorecon=paths.RESTORECON):
        """
        We don't have a specific package requirement for policycoreutils
        which provides restorecon. This is because we don't require
        SELinux on client installs. However if SELinux is enabled then
        this package is required.

        This function returns nothing but may raise a Runtime exception
        if SELinux is enabled but restorecon is not available.
        """
        if not selinux_enabled():
            return

        if not os.path.exists(restorecon):
            raise RuntimeError('SELinux is enabled but %s does not exist.\n'
                               'Install the policycoreutils package and start '
                               'the installation again.' % restorecon)

    def check_ipv6_stack_enabled(self):
        """Checks whether IPv6 kernel module is loaded.

        Function checks if /proc/net/if_inet6 is present. If IPv6 stack is
        enabled, it exists and contains the interfaces configuration.

        :raises: RuntimeError when IPv6 stack is disabled
        """
        if not os.path.exists(paths.IF_INET6):
            raise RuntimeError(
                "IPv6 kernel module has to be enabled. If you do not wish to "
                "use IPv6, please disable it on the interfaces in "
                "sysctl.conf and enable the IPv6 kernel module.")

    def restore_pre_ipa_client_configuration(self, fstore, statestore,
                                             was_sssd_installed,
                                             was_sssd_configured):

        auth_config = RedHatAuthConfig()
        if statestore.has_state('authconfig'):
            # disable only those configurations that we enabled during install
            for conf in ('ldap', 'krb5', 'sssd', 'sssdauth', 'mkhomedir'):
                cnf = statestore.restore_state('authconfig', conf)
                # Do not disable sssd, as this can cause issues with its later
                # uses. Remove it from statestore however, so that it becomes
                # empty at the end of uninstall process.
                if cnf and conf != 'sssd':
                    auth_config.disable(conf)
        else:
            # There was no authconfig status store
            # It means the code was upgraded after original install
            # Fall back to old logic
            auth_config.disable("ldap")
            auth_config.disable("krb5")
            if not(was_sssd_installed and was_sssd_configured):
                # Only disable sssdauth. Disabling sssd would cause issues
                # with its later uses.
                auth_config.disable("sssdauth")
            auth_config.disable("mkhomedir")

        auth_config.execute()

    def set_nisdomain(self, nisdomain):
        # Let authconfig setup the permanent configuration
        auth_config = RedHatAuthConfig()
        auth_config.add_parameter("nisdomain", nisdomain)
        auth_config.execute()

    def modify_nsswitch_pam_stack(self, sssd, mkhomedir, statestore):
        auth_config = RedHatAuthConfig()

        if sssd:
            statestore.backup_state('authconfig', 'sssd', True)
            statestore.backup_state('authconfig', 'sssdauth', True)
            auth_config.enable("sssd")
            auth_config.enable("sssdauth")
        else:
            statestore.backup_state('authconfig', 'ldap', True)
            auth_config.enable("ldap")
            auth_config.enable("forcelegacy")

        if mkhomedir:
            statestore.backup_state('authconfig', 'mkhomedir', True)
            auth_config.enable("mkhomedir")

        auth_config.execute()

    def modify_pam_to_use_krb5(self, statestore):
        auth_config = RedHatAuthConfig()
        statestore.backup_state('authconfig', 'krb5', True)
        auth_config.enable("krb5")
        auth_config.add_option("nostart")
        auth_config.execute()

    def backup_auth_configuration(self, path):
        auth_config = RedHatAuthConfig()
        auth_config.backup(path)

    def restore_auth_configuration(self, path):
        auth_config = RedHatAuthConfig()
        auth_config.restore(path)

    def reload_systemwide_ca_store(self):
        try:
            ipautil.run([paths.UPDATE_CA_TRUST])
        except CalledProcessError as e:
            root_logger.error(
                "Could not update systemwide CA trust database: %s", e)
            return False
        else:
            root_logger.info("Systemwide CA database updated.")
            return True

    def insert_ca_certs_into_systemwide_ca_store(self, ca_certs):
        # pylint: disable=ipa-forbidden-import
        from ipalib import x509  # FixMe: break import cycle
        from ipalib.errors import CertificateError
        # pylint: enable=ipa-forbidden-import

        new_cacert_path = paths.SYSTEMWIDE_IPA_CA_CRT

        if os.path.exists(new_cacert_path):
            try:
                os.remove(new_cacert_path)
            except OSError as e:
                root_logger.error(
                    "Could not remove %s: %s", new_cacert_path, e)
                return False

        new_cacert_path = paths.IPA_P11_KIT

        try:
            f = open(new_cacert_path, 'w')
        except IOError as e:
            root_logger.info("Failed to open %s: %s" % (new_cacert_path, e))
            return False

        f.write("# This file was created by IPA. Do not edit.\n"
                "\n")

        has_eku = set()
        for cert, nickname, trusted, ext_key_usage in ca_certs:
            try:
                subject = x509.get_der_subject(cert, x509.DER)
                issuer = x509.get_der_issuer(cert, x509.DER)
                serial_number = x509.get_der_serial_number(cert, x509.DER)
                public_key_info = x509.get_der_public_key_info(cert, x509.DER)
            except (PyAsn1Error, ValueError, CertificateError) as e:
                root_logger.warning(
                    "Failed to decode certificate \"%s\": %s", nickname, e)
                continue

            label = urllib.parse.quote(nickname)
            subject = urllib.parse.quote(subject)
            issuer = urllib.parse.quote(issuer)
            serial_number = urllib.parse.quote(serial_number)
            public_key_info = urllib.parse.quote(public_key_info)

            cert = base64.b64encode(cert)
            cert = x509.make_pem(cert)

            obj = ("[p11-kit-object-v1]\n"
                   "class: certificate\n"
                   "certificate-type: x-509\n"
                   "certificate-category: authority\n"
                   "label: \"%(label)s\"\n"
                   "subject: \"%(subject)s\"\n"
                   "issuer: \"%(issuer)s\"\n"
                   "serial-number: \"%(serial_number)s\"\n"
                   "x-public-key-info: \"%(public_key_info)s\"\n" %
                   dict(label=label,
                        subject=subject,
                        issuer=issuer,
                        serial_number=serial_number,
                        public_key_info=public_key_info))
            if trusted is True:
                obj += "trusted: true\n"
            elif trusted is False:
                obj += "x-distrusted: true\n"
            obj += "%s\n\n" % cert
            f.write(obj)

            if ext_key_usage is not None and public_key_info not in has_eku:
                if not ext_key_usage:
                    ext_key_usage = {x509.EKU_PLACEHOLDER}
                try:
                    ext_key_usage = x509.encode_ext_key_usage(ext_key_usage)
                except PyAsn1Error as e:
                    root_logger.warning(
                        "Failed to encode extended key usage for \"%s\": %s",
                        nickname, e)
                    continue
                value = urllib.parse.quote(ext_key_usage)
                obj = ("[p11-kit-object-v1]\n"
                       "class: x-certificate-extension\n"
                       "label: \"ExtendedKeyUsage for %(label)s\"\n"
                       "x-public-key-info: \"%(public_key_info)s\"\n"
                       "object-id: 2.5.29.37\n"
                       "value: \"%(value)s\"\n\n" %
                       dict(label=label,
                            public_key_info=public_key_info,
                            value=value))
                f.write(obj)
                has_eku.add(public_key_info)

        f.close()

        # Add the CA to the systemwide CA trust database
        if not self.reload_systemwide_ca_store():
            return False

        return True

    def remove_ca_certs_from_systemwide_ca_store(self):
        result = True
        update = False

        # Remove CA cert from systemwide store
        for new_cacert_path in (paths.IPA_P11_KIT,
                                paths.SYSTEMWIDE_IPA_CA_CRT):
            if not os.path.exists(new_cacert_path):
                continue
            try:
                os.remove(new_cacert_path)
            except OSError as e:
                root_logger.error(
                    "Could not remove %s: %s", new_cacert_path, e)
                result = False
            else:
                update = True

        if update:
            if not self.reload_systemwide_ca_store():
                return False

        return result

    def backup_hostname(self, fstore, statestore):
        filepath = paths.ETC_HOSTNAME
        if os.path.exists(filepath):
            fstore.backup_file(filepath)

        # store old hostname
        old_hostname = socket.gethostname()
        statestore.backup_state('network', 'hostname', old_hostname)

    def restore_hostname(self, fstore, statestore):
        old_hostname = statestore.get_state('network', 'hostname')

        if old_hostname is not None:
            try:
                self.set_hostname(old_hostname)
            except ipautil.CalledProcessError as e:
                root_logger.debug(traceback.format_exc())
                root_logger.error(
                    "Failed to restore this machine hostname to %s (%s).",
                    old_hostname, e
                )

        filepath = paths.ETC_HOSTNAME
        if fstore.has_file(filepath):
            fstore.restore_file(filepath)


    def set_selinux_booleans(self, required_settings, backup_func=None):
        def get_setsebool_args(changes):
            args = [paths.SETSEBOOL, "-P"]
            args.extend(["%s=%s" % update for update in changes.items()])

            return args

        if not selinux_enabled():
            return False

        updated_vars = {}
        failed_vars = {}
        for setting, state in required_settings.items():
            if state is None:
                continue
            try:
                result = ipautil.run(
                    [paths.GETSEBOOL, setting],
                    capture_output=True
                )
                original_state = result.output.split()[2]
                if backup_func is not None:
                    backup_func(setting, original_state)

                if original_state != state:
                    updated_vars[setting] = state
            except ipautil.CalledProcessError as e:
                log.error("Cannot get SELinux boolean '%s': %s", setting, e)
                failed_vars[setting] = state

        if updated_vars:
            args = get_setsebool_args(updated_vars)
            try:
                ipautil.run(args)
            except ipautil.CalledProcessError:
                failed_vars.update(updated_vars)

        if failed_vars:
            raise ipapython.errors.SetseboolError(
                failed=failed_vars,
                command=' '.join(get_setsebool_args(failed_vars)))

        return True

    def parse_ipa_version(self, version):
        """
        :param version: textual version
        :return: object implementing proper __cmp__ method for version compare
        """
        return IPAVersion(version)

    def configure_httpd_service_ipa_conf(self):
        """Create systemd config for httpd service to work with IPA
        """
        if not os.path.exists(paths.SYSTEMD_SYSTEM_HTTPD_D_DIR):
            os.mkdir(paths.SYSTEMD_SYSTEM_HTTPD_D_DIR, 0o755)

        ipautil.copy_template_file(
            os.path.join(paths.USR_SHARE_IPA_DIR, 'ipa-httpd.conf.template'),
            paths.SYSTEMD_SYSTEM_HTTPD_IPA_CONF,
            dict(
                KDCPROXY_CONFIG=paths.KDCPROXY_CONFIG,
                IPA_HTTPD_KDCPROXY=paths.IPA_HTTPD_KDCPROXY,
                KRB5CC_HTTPD=paths.KRB5CC_HTTPD,
            )
        )

        os.chmod(paths.SYSTEMD_SYSTEM_HTTPD_IPA_CONF, 0o644)
        self.restore_context(paths.SYSTEMD_SYSTEM_HTTPD_IPA_CONF)

        ipautil.run([paths.SYSTEMCTL, "--system", "daemon-reload"],
                    raiseonerr=False)

    def configure_http_gssproxy_conf(self):
        ipautil.copy_template_file(
            os.path.join(paths.USR_SHARE_IPA_DIR, 'gssproxy.conf.template'),
            paths.GSSPROXY_CONF,
            dict(
                HTTP_KEYTAB=paths.HTTP_KEYTAB,
                HTTP_CCACHE=paths.HTTP_CCACHE,
                HTTPD_USER=constants.HTTPD_USER,
                IPAAPI_USER=IPAAPI_USER,
            )
        )

        os.chmod(paths.GSSPROXY_CONF, 0o600)
        self.restore_context(paths.GSSPROXY_CONF)

    def remove_httpd_service_ipa_conf(self):
        """Remove systemd config for httpd service of IPA"""
        try:
            os.unlink(paths.SYSTEMD_SYSTEM_HTTPD_IPA_CONF)
        except OSError as e:
            if e.errno == errno.ENOENT:
                root_logger.debug(
                    'Trying to remove %s but file does not exist',
                    paths.SYSTEMD_SYSTEM_HTTPD_IPA_CONF
                )
            else:
                root_logger.error(
                    'Error removing %s: %s',
                    paths.SYSTEMD_SYSTEM_HTTPD_IPA_CONF, e
                )
            return

        ipautil.run([paths.SYSTEMCTL, "--system", "daemon-reload"],
                    raiseonerr=False)

    def set_hostname(self, hostname):
        ipautil.run([paths.BIN_HOSTNAMECTL, 'set-hostname', hostname])

    def is_fips_enabled(self):
        """
        Checks whether this host is FIPS-enabled.

        Returns a boolean indicating if the host is FIPS-enabled, i.e. if the
        file /proc/sys/crypto/fips_enabled contains a non-0 value. Otherwise,
        or if the file /proc/sys/crypto/fips_enabled does not exist,
        the function returns False.
        """
        try:
            with open(paths.PROC_FIPS_ENABLED, 'r') as f:
                if f.read().strip() != '0':
                    return True
        except IOError:
            # Consider that the host is not fips-enabled if the file does not
            # exist
            pass
        return False

    def _create_tmpfiles_dir(self, name, mode, uid, gid):
        if not os.path.exists(name):
            os.mkdir(name)
        os.chmod(name, mode)
        os.chown(name, uid, gid)

    def create_tmpfiles_dirs(self):
        parent = os.path.dirname(paths.IPA_CCACHES)
        pent = pwd.getpwnam(IPAAPI_USER)
        self._create_tmpfiles_dir(parent, 0o711, 0, 0)
        self._create_tmpfiles_dir(paths.IPA_CCACHES, 0o770,
                                  pent.pw_uid, pent.pw_gid)

    def configure_tmpfiles(self):
        shutil.copy(
            os.path.join(paths.USR_SHARE_IPA_DIR, 'ipa.conf.tmpfiles'),
            paths.ETC_TMPFILESD_IPA
        )


tasks = RedHatTaskNamespace()
