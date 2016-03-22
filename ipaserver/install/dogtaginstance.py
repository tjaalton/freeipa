# Authors: Ade Lee <alee@redhat.com>
#
# Copyright (C) 2014  Red Hat
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
#

import base64
import binascii
import ldap
import os
import shutil
import tempfile
import traceback
import dbus
import pwd

from pki.client import PKIConnection
import pki.system

from ipalib import errors

from ipaplatform import services
from ipaplatform.constants import constants
from ipaplatform.paths import paths
from ipapython import certmonger
from ipapython import ipaldap
from ipapython import ipautil
from ipapython.dn import DN
from ipaserver.install import service
from ipaserver.install import installutils
from ipaserver.install import replication
from ipaserver.install.installutils import stopped_service
from ipapython.ipa_log_manager import log_mgr

PKI_USER = constants.PKI_USER
HTTPD_USER = constants.HTTPD_USER


def get_security_domain():
    """
    Get the security domain from the REST interface on the local Dogtag CA
    This function will succeed if the local dogtag CA is up.
    """
    connection = PKIConnection()
    domain_client = pki.system.SecurityDomainClient(connection)
    info = domain_client.get_security_domain_info()
    return info


def is_installing_replica(sys_type):
    """
    We expect only one of each type of Dogtag subsystem in an IPA deployment.
    That means that if a subsystem of the specified type has already been
    deployed - and therefore appears in the security domain - then we must be
    installing a replica.
    """
    info = get_security_domain()
    try:
        sys_list = info.systems[sys_type]
        return len(sys_list.hosts) > 0
    except KeyError:
        return False


def export_kra_agent_pem():
    """
    Export ipaCert with private key for client authentication.
    """
    fd, filename = tempfile.mkstemp(dir=paths.HTTPD_ALIAS_DIR)
    os.close(fd)

    args = ["/usr/bin/pki",
            "-d", paths.HTTPD_ALIAS_DIR,
            "-C", paths.ALIAS_PWDFILE_TXT,
            "client-cert-show", "ipaCert",
            "--client-cert", filename]
    ipautil.run(args)

    pent = pwd.getpwnam(HTTPD_USER)
    os.chown(filename, 0, pent.pw_gid)
    os.chmod(filename, 0o440)

    os.rename(filename, paths.KRA_AGENT_PEM)


class DogtagInstance(service.Service):
    """
    This is the base class for a Dogtag 10+ instance, which uses a
    shared tomcat instance and DS to host the relevant subsystems.

    It contains functions that will be common to installations of the
    CA, KRA, and eventually TKS and TPS.
    """

    tracking_reqs = None
    server_cert_name = None

    def __init__(self, realm, subsystem, service_desc, host_name=None,
                 dm_password=None, ldapi=True,
                 nss_db=paths.PKI_TOMCAT_ALIAS_DIR):
        """Initializer"""

        super(DogtagInstance, self).__init__(
            'pki-tomcatd',
            service_desc=service_desc,
            dm_password=dm_password,
            ldapi=ldapi
        )

        self.realm = realm
        self.admin_password = None
        self.fqdn = host_name
        self.pkcs12_info = None
        self.clone = False

        self.basedn = DN(('o', 'ipa%s' % subsystem.lower()))
        self.admin_user = "admin"
        self.admin_dn = DN(('uid', self.admin_user),
                           ('ou', 'people'), ('o', 'ipaca'))
        self.admin_groups = None
        self.agent_db = tempfile.mkdtemp(prefix="tmp-")
        self.subsystem = subsystem
        self.security_domain_name = "IPA"
        # replication parameters
        self.master_host = None
        self.master_replication_port = None
        self.subject_base = None
        self.nss_db = nss_db

        self.log = log_mgr.get_logger(self)

    def __del__(self):
        shutil.rmtree(self.agent_db, ignore_errors=True)

    def is_installed(self):
        """
        Determine if subsystem instance has been installed.

        Returns True/False
        """
        return os.path.exists(os.path.join(
            paths.VAR_LIB_PKI_TOMCAT_DIR, self.subsystem.lower()))

    def spawn_instance(self, cfg_file, nolog_list=None):
        """
        Create and configure a new Dogtag instance using pkispawn.
        Passes in a configuration file with IPA-specific
        parameters.
        """
        subsystem = self.subsystem

        # Define the things we don't want logged
        if nolog_list is None:
            nolog_list = []
        nolog = tuple(nolog_list) + (self.admin_password, self.dm_password)

        args = [paths.PKISPAWN,
                "-s", subsystem,
                "-f", cfg_file]

        with open(cfg_file) as f:
            self.log.debug(
                'Contents of pkispawn configuration file (%s):\n%s',
                cfg_file, ipautil.nolog_replace(f.read(), nolog))

        try:
            ipautil.run(args, nolog=nolog)
        except ipautil.CalledProcessError as e:
            self.handle_setup_error(e)

    def restart_instance(self):
        try:
            self.restart('pki-tomcat')
        except Exception:
            self.log.debug(traceback.format_exc())
            self.log.critical(
                "Failed to restart the Dogtag instance."
                "See the installation log for details.")

    def start_instance(self):
        try:
            self.start('pki-tomcat')
        except Exception:
            self.log.debug(traceback.format_exc())
            self.log.critical(
                "Failed to restart the Dogtag instance."
                "See the installation log for details.")

    def stop_instance(self):
        try:
            self.stop('pki-tomcat')
        except Exception:
            self.log.debug(traceback.format_exc())
            self.log.critical(
                "Failed to restart the Dogtag instance."
                "See the installation log for details.")

    def enable_client_auth_to_db(self, config):
        """
        Enable client auth connection to the internal db.
        Path to CS.cfg config file passed in.
        """

        with stopped_service('pki-tomcatd', 'pki-tomcat'):
            installutils.set_directive(
                config,
                'authz.instance.DirAclAuthz.ldap.ldapauth.authtype',
                'SslClientAuth', quotes=False, separator='=')
            installutils.set_directive(
                config,
                'authz.instance.DirAclAuthz.ldap.ldapauth.clientCertNickname',
                'subsystemCert cert-pki-ca', quotes=False, separator='=')
            installutils.set_directive(
                config,
                'authz.instance.DirAclAuthz.ldap.ldapconn.port', '636',
                quotes=False, separator='=')
            installutils.set_directive(
                config,
                'authz.instance.DirAclAuthz.ldap.ldapconn.secureConn',
                'true', quotes=False, separator='=')

            installutils.set_directive(
                config,
                'internaldb.ldapauth.authtype',
                'SslClientAuth', quotes=False, separator='=')

            installutils.set_directive(
                config,
                'internaldb.ldapauth.clientCertNickname',
                'subsystemCert cert-pki-ca', quotes=False, separator='=')
            installutils.set_directive(
                config,
                'internaldb.ldapconn.port', '636', quotes=False, separator='=')
            installutils.set_directive(
                config,
                'internaldb.ldapconn.secureConn', 'true', quotes=False,
                separator='=')
            # Remove internaldb password as is not needed anymore
            installutils.set_directive(paths.PKI_TOMCAT_PASSWORD_CONF,
                                       'internaldb', None)

    def uninstall(self):
        if self.is_installed():
            self.print_msg("Unconfiguring %s" % self.subsystem)

        try:
            ipautil.run([paths.PKIDESTROY,
                         "-i", 'pki-tomcat',
                         "-s", self.subsystem])
        except ipautil.CalledProcessError as e:
            self.log.critical("failed to uninstall %s instance %s",
                              self.subsystem, e)

    def http_proxy(self):
        """ Update the http proxy file  """
        template_filename = ipautil.SHARE_DIR + "ipa-pki-proxy.conf"
        sub_dict = dict(
            DOGTAG_PORT=8009,
            CLONE='' if self.clone else '#',
            FQDN=self.fqdn,
        )
        template = ipautil.template_file(template_filename, sub_dict)
        with open(paths.HTTPD_IPA_PKI_PROXY_CONF, "w") as fd:
            fd.write(template)

    def configure_certmonger_renewal(self):
        """
        Create a new CA type for certmonger that will retrieve updated
        certificates from the dogtag master server.
        """
        cmonger = services.knownservices.certmonger
        cmonger.enable()
        services.knownservices.messagebus.start()
        cmonger.start()

        bus = dbus.SystemBus()
        obj = bus.get_object('org.fedorahosted.certmonger',
                             '/org/fedorahosted/certmonger')
        iface = dbus.Interface(obj, 'org.fedorahosted.certmonger')
        path = iface.find_ca_by_nickname('dogtag-ipa-ca-renew-agent')
        if not path:
            iface.add_known_ca(
                'dogtag-ipa-ca-renew-agent',
                paths.DOGTAG_IPA_CA_RENEW_AGENT_SUBMIT,
                dbus.Array([], dbus.Signature('s')))

    def __get_pin(self):
        try:
            return certmonger.get_pin('internal')
        except IOError as e:
            self.log.debug(
                'Unable to determine PIN for the Dogtag instance: %s', e)
            raise RuntimeError(e)

    def configure_renewal(self):
        """ Configure certmonger to renew system certs """
        pin = self.__get_pin()

        for nickname, profile in self.tracking_reqs:
            try:
                certmonger.dogtag_start_tracking(
                    ca='dogtag-ipa-ca-renew-agent',
                    nickname=nickname,
                    pin=pin,
                    pinfile=None,
                    secdir=self.nss_db,
                    pre_command='stop_pkicad',
                    post_command='renew_ca_cert "%s"' % nickname,
                    profile=profile)
            except RuntimeError as e:
                self.log.error(
                    "certmonger failed to start tracking certificate: %s", e)

    def track_servercert(self):
        """
        Specifically do not tell certmonger to restart the CA. This will be
        done by the renewal script, renew_ca_cert once all the subsystem
        certificates are renewed.
        """
        pin = self.__get_pin()
        try:
            certmonger.dogtag_start_tracking(
                ca='dogtag-ipa-renew-agent',
                nickname=self.server_cert_name,
                pin=pin,
                pinfile=None,
                secdir=self.nss_db,
                pre_command='stop_pkicad',
                post_command='renew_ca_cert "%s"' % self.server_cert_name)
        except RuntimeError as e:
            self.log.error(
                "certmonger failed to start tracking certificate: %s" % e)

    def stop_tracking_certificates(self, stop_certmonger=True):
        """Stop tracking our certificates. Called on uninstall.
        """
        self.print_msg(
            "Configuring certmonger to stop tracking system certificates "
            "for %s" % self.subsystem)

        cmonger = services.knownservices.certmonger
        services.knownservices.messagebus.start()
        cmonger.start()

        nicknames = [nickname for nickname, profile in self.tracking_reqs]
        if self.server_cert_name is not None:
            nicknames.append(self.server_cert_name)

        for nickname in nicknames:
            try:
                certmonger.stop_tracking(
                    self.nss_db, nickname=nickname)
            except RuntimeError as e:
                self.log.error(
                    "certmonger failed to stop tracking certificate: %s", e)

        if stop_certmonger:
            cmonger.stop()

    @staticmethod
    def update_cert_cs_cfg(nickname, cert, directives, cs_cfg):
        """
        When renewing a Dogtag subsystem certificate the configuration file
        needs to get the new certificate as well.

        nickname is one of the known nicknames.
        cert is a DER-encoded certificate.
        directives is the list of directives to be updated for the subsystem
        cs_cfg is the path to the CS.cfg file
        """

        with stopped_service('pki-tomcatd', 'pki-tomcat'):
            installutils.set_directive(
                cs_cfg,
                directives[nickname],
                base64.b64encode(cert),
                quotes=False,
                separator='=')

    def get_admin_cert(self):
        """
        Get the certificate for the admin user by checking the ldap entry
        for the user.  There should be only one certificate per user.
        """
        self.log.debug('Trying to find the certificate for the admin user')
        conn = None

        try:
            conn = ipaldap.IPAdmin(self.fqdn, ldapi=True, realm=self.realm)
            conn.do_external_bind('root')

            entry_attrs = conn.get_entry(self.admin_dn, ['usercertificate'])
            admin_cert = entry_attrs.get('usercertificate')[0]

            # TODO(edewata) Add check to warn if there is more than one cert.
        finally:
            if conn is not None:
                conn.unbind()

        return base64.b64encode(admin_cert)

    def handle_setup_error(self, e):
        self.log.critical("Failed to configure %s instance: %s"
                          % (self.subsystem, e))
        self.log.critical("See the installation logs and the following "
                          "files/directories for more information:")
        self.log.critical("  %s" % paths.TOMCAT_TOPLEVEL_DIR)

        raise RuntimeError("%s configuration failed." % self.subsystem)

    def __add_admin_to_group(self, group):
        dn = DN(('cn', group), ('ou', 'groups'), ('o', 'ipaca'))
        entry = self.admin_conn.get_entry(dn)
        members = entry.get('uniqueMember', [])
        members.append(self.admin_dn)
        mod = [(ldap.MOD_REPLACE, 'uniqueMember', members)]
        try:
            self.admin_conn.modify_s(dn, mod)
        except ldap.TYPE_OR_VALUE_EXISTS:
            # already there
            pass

    def setup_admin(self):
        self.admin_user = "admin-%s" % self.fqdn
        self.admin_password = binascii.hexlify(os.urandom(16))

        if not self.admin_conn:
            self.ldap_connect()

        self.admin_dn = DN(('uid', self.admin_user),
                           ('ou', 'people'), ('o', 'ipaca'))

        # remove user if left-over exists
        try:
            entry = self.admin_conn.delete_entry(self.admin_dn)
        except errors.NotFound:
            pass

        # add user
        entry = self.admin_conn.make_entry(
            self.admin_dn,
            objectclass=["top", "person", "organizationalPerson",
                         "inetOrgPerson", "cmsuser"],
            uid=[self.admin_user],
            cn=[self.admin_user],
            sn=[self.admin_user],
            usertype=['adminType'],
            mail=['root@localhost'],
            userPassword=[self.admin_password],
            userstate=['1']
        )
        self.admin_conn.add_entry(entry)

        for group in self.admin_groups:
            self.__add_admin_to_group(group)

        # Now wait until the other server gets replicated this data
        master_conn = ipaldap.IPAdmin(self.master_host,
                                      port=389,
                                      protocol='ldap')
        master_conn.do_sasl_gssapi_bind()
        replication.wait_for_entry(master_conn, entry)
        del master_conn

    def __remove_admin_from_group(self, group):
        dn = DN(('cn', group), ('ou', 'groups'), ('o', 'ipaca'))
        entry = self.admin_conn.get_entry(dn)
        mod = [(ldap.MOD_DELETE, 'uniqueMember', self.admin_dn)]
        try:
            self.admin_conn.modify_s(dn, mod)
        except ldap.NO_SUCH_ATTRIBUTE:
            # already removed
            pass

    def teardown_admin(self):

        if not self.admin_conn:
            self.ldap_connect()

        for group in self.admin_groups:
            self.__remove_admin_from_group(group)
        self.admin_conn.delete_entry(self.admin_dn)

    def _use_ldaps_during_spawn(self, config, ds_cacert=paths.IPA_CA_CRT):
        config.set(self.subsystem, "pki_ds_ldaps_port", "636")
        config.set(self.subsystem, "pki_ds_secure_connection", "True")
        config.set(self.subsystem, "pki_ds_secure_connection_ca_pem_file",
                   ds_cacert)
