# Authors: Rob Crittenden <rcritten@redhat.com>
#          Ade Lee <alee@redhat.com>
#          Andrew Wnuk <awnuk@redhat.com>
#
# Copyright (C) 2009  Red Hat
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

from __future__ import print_function, absolute_import

import base64
import binascii
import logging

import dbus
import enum
import ldap
import os
import pwd
import grp
import re
import shutil
import sys
import syslog
import time
import tempfile

import six
# pylint: disable=import-error
from six.moves.configparser import RawConfigParser
# pylint: enable=import-error
from pyasn1.codec.der import encoder
from pyasn1.type import char, univ, namedtype
import pyasn1.error

from ipalib import api
from ipalib import x509
from ipalib import errors
import ipalib.constants
from ipalib.install import certmonger
from ipaplatform import services
from ipaplatform.paths import paths
from ipaplatform.tasks import tasks

from ipapython import directivesetter
from ipapython import dogtag
from ipapython import ipautil
from ipapython.certdb import get_ca_nickname
from ipapython.dn import DN
from ipapython.ipa_log_manager import standard_logging_setup
from ipaserver.secrets.kem import IPAKEMKeys

from ipaserver.install import certs
from ipaserver.install import dsinstance
from ipaserver.install import installutils
from ipaserver.install import ldapupdate
from ipaserver.install import replication
from ipaserver.install import sysupgrade
from ipaserver.install.dogtaginstance import DogtagInstance
from ipaserver.plugins import ldap2
from ipaserver.masters import ENABLED_SERVICE
from ipaserver.install.installutils import remove_file

logger = logging.getLogger(__name__)

# We need to reset the template because the CA uses the regular boot
# information
INF_TEMPLATE = """
[General]
FullMachineName=   $FQDN
SuiteSpotUserID=   $USER
SuiteSpotGroup=    $GROUP
ServerRoot=    $SERVER_ROOT
[slapd]
ServerPort=   $DSPORT
ServerIdentifier=   $SERVERID
Suffix=   $SUFFIX
RootDN=   cn=Directory Manager
RootDNPwd= $PASSWORD
ConfigFile = /usr/share/pki/ca/conf/database.ldif
"""


ADMIN_GROUPS = [
    'Enterprise CA Administrators',
    'Enterprise KRA Administrators',
    'Security Domain Administrators'
]


class ExternalCAType(enum.Enum):
    GENERIC = 'generic'
    MS_CS = 'ms-cs'


def check_ports():
    """Check that dogtag ports (8080, 8443) are available.

    Returns True when ports are free, False if they are taken.
    """
    return all([ipautil.check_port_bindable(8443),
                ipautil.check_port_bindable(8080)])


def get_preop_pin(instance_root, instance_name):
    # Only used for Dogtag 9
    preop_pin = None

    filename = instance_root + "/" + instance_name + "/conf/CS.cfg"

    # read the config file and get the preop pin
    try:
        f = open(filename)
    except IOError as e:
        logger.error("Cannot open configuration file.%s", str(e))
        raise e
    data = f.read()
    data = data.split('\n')
    pattern = re.compile("preop.pin=(.*)")
    for line in data:
        match = re.search(pattern, line)
        if match:
            preop_pin = match.group(1)
            break

    if preop_pin is None:
        raise RuntimeError(
            "Unable to find preop.pin in %s. Is your CA already configured?" %
            filename)

    return preop_pin


def import_pkcs12(input_file, input_passwd, cert_database,
                  cert_passwd):
    ipautil.run([paths.PK12UTIL, "-d", cert_database,
                 "-i", input_file,
                 "-k", cert_passwd,
                 "-w", input_passwd])


def get_value(s):
    """
    Parse out a name/value pair from a Javascript variable.
    """
    try:
        expr = s.split('=', 1)
        value = expr[1]
        value = value.replace('\"', '')
        value = value.replace(';', '')
        value = value.replace('\\n', '\n')
        value = value.replace('\\r', '\r')
        return value
    except IndexError:
        return None


def find_substring(data, value):
    """
    Scan through a list looking for a string that starts with value.
    """
    for d in data:
        if d.startswith(value):
            return get_value(d)
    return None


def get_defList(data):
    """
    Return a dictionary of defList name/value pairs.

    A certificate signing request is specified as a series of these.
    """
    varname = None
    value = None
    skip = False
    defdict = {}
    for d in data:
        if d.startswith("defList = new Object"):
            varname = None
            value = None
            skip = False
        if d.startswith("defList.defId"):
            varname = get_value(d)
        if d.startswith("defList.defVal"):
            value = get_value(d)
            if skip:
                varname = None
                value = None
                skip = False
        if d.startswith("defList.defConstraint"):
            ctype = get_value(d)
            if ctype == "readonly":
                skip = True

        if varname and value:
            defdict[varname] = value
            varname = None
            value = None

    return defdict


def get_outputList(data):
    """
    Return a dictionary of outputList name/value pairs.

    The output from issuing a certificate is a series of these.
    """
    varname = None
    value = None
    outputdict = {}
    for d in data:
        if d.startswith("outputList = new"):
            varname = None
            value = None
        if d.startswith("outputList.outputId"):
            varname = get_value(d)
        if d.startswith("outputList.outputVal"):
            value = get_value(d)

        if varname and value:
            outputdict[varname] = value
            varname = None
            value = None

    return outputdict


def get_crl_files(path=None):
    """
    Traverse dogtag's CRL files in default CRL publish directory or in chosen
    target directory.

    @param path Custom target directory
    """
    if path is None:
        path = paths.PKI_CA_PUBLISH_DIR

    files = os.listdir(path)
    for f in files:
        if f == "MasterCRL.bin":
            yield os.path.join(path, f)
        elif f.endswith(".der"):
            yield os.path.join(path, f)


def is_step_one_done():
    """Read CS.cfg and determine if step one of an external CA install is done
    """
    path = paths.CA_CS_CFG_PATH
    if not os.path.exists(path):
        return False
    test = directivesetter.get_directive(path, 'preop.ca.type', '=')
    if test == "otherca":
        return True
    return False


def is_ca_installed_locally():
    """Check if CA is installed locally by checking for existence of CS.cfg
    :return:True/False
    """
    return os.path.exists(paths.CA_CS_CFG_PATH)


class InconsistentCRLGenConfigException(Exception):
    pass


class CAInstance(DogtagInstance):
    """
    When using a dogtag CA the DS database contains just the
    server cert for DS. The mod_nss database will contain the RA agent
    cert that will be used to do authenticated requests against dogtag.

    This is done because we use python-nss and will inherit the opened
    NSS database in mod_python. In nsslib.py we do an nssinit but this will
    return success if the database is already initialized. It doesn't care
    if the database is different or not.

    external is a state machine:
       0 = not an externally signed CA
       1 = generating CSR to be signed
       2 = have signed cert, continue installation
    """

    tracking_reqs = ('auditSigningCert cert-pki-ca',
                     'ocspSigningCert cert-pki-ca',
                     'subsystemCert cert-pki-ca',
                     'caSigningCert cert-pki-ca')
    server_cert_name = 'Server-Cert cert-pki-ca'
    # The following must be aligned with the RewriteRule defined in
    # install/share/ipa-pki-proxy.conf.template
    crl_rewrite_pattern = r"^\s*(RewriteRule\s+\^/ipa/crl/MasterCRL.bin\s.*)$"
    crl_rewrite_comment = r"^#\s*RewriteRule\s+\^/ipa/crl/MasterCRL.bin\s.*$"
    crl_rewriterule = "\nRewriteRule ^/ipa/crl/MasterCRL.bin " \
        "http://{}/ca/ee/ca/getCRL?" \
        "op=getCRL&crlIssuingPoint=MasterCRL " \
        "[L,R=301,NC]"

    def __init__(self, realm=None, host_name=None, custodia=None):
        super(CAInstance, self).__init__(
            realm=realm,
            subsystem="CA",
            service_desc="certificate server",
            host_name=host_name,
            service_prefix=ipalib.constants.PKI_GSSAPI_SERVICE_NAME,
            config=paths.CA_CS_CFG_PATH,
        )

        # for external CAs
        self.external = 0
        self.csr_file = None
        self.cert_file = None
        self.cert_chain_file = None

        if realm is not None:
            self.canickname = get_ca_nickname(realm)
        else:
            self.canickname = None
        self.ra_cert = None
        self.requestId = None
        self.no_db_setup = False
        self.keytab = os.path.join(
            paths.PKI_TOMCAT, self.service_prefix + '.keytab')
        # Custodia instance for RA key retrieval
        self._custodia = custodia

    def configure_instance(self, host_name, dm_password, admin_password,
                           pkcs12_info=None, master_host=None, csr_file=None,
                           cert_file=None, cert_chain_file=None,
                           master_replication_port=None,
                           subject_base=None, ca_subject=None,
                           ca_signing_algorithm=None,
                           ca_type=None, external_ca_profile=None,
                           ra_p12=None, ra_only=False,
                           promote=False, use_ldaps=False):
        """Create a CA instance.

           To create a clone, pass in pkcs12_info.

           Creating a CA with an external signer is a 2-step process. In
           step 1 we generate a CSR. In step 2 we are given the cert and
           chain and actually proceed to create the CA. For step 1 set
           csr_file. For step 2 set cert_file and cert_chain_file.
        """
        self.fqdn = host_name
        self.dm_password = dm_password
        self.admin_user = "admin"
        self.admin_groups = ADMIN_GROUPS

        # NOTE: "admin_password" refers to the password for PKI
        # "admin" account.  This is not necessarily the same as
        # the IPA admin password.  Indeed, ca.configure_instance
        # gets called with admin_password=dm_password.
        #
        self.admin_password = admin_password

        self.pkcs12_info = pkcs12_info
        if self.pkcs12_info is not None:
            self.clone = True
        self.master_host = master_host
        self.master_replication_port = master_replication_port
        self.ra_p12 = ra_p12

        self.subject_base = \
            subject_base or installutils.default_subject_base(self.realm)
        self.ca_subject = \
            ca_subject or installutils.default_ca_subject_dn(self.subject_base)

        if ca_signing_algorithm is None:
            self.ca_signing_algorithm = 'SHA256withRSA'
        else:
            self.ca_signing_algorithm = ca_signing_algorithm
        if ca_type is not None:
            self.ca_type = ca_type
        else:
            self.ca_type = ExternalCAType.GENERIC.value
        self.external_ca_profile = external_ca_profile

        self.no_db_setup = promote
        self.use_ldaps = use_ldaps

        # Determine if we are installing as an externally-signed CA and
        # what stage we're in.
        if csr_file is not None:
            self.csr_file = csr_file
            self.external = 1
        elif cert_file is not None:
            self.cert_file = cert_file
            self.cert_chain_file = cert_chain_file
            self.external = 2

        if self.clone:
            has_ra_cert = os.path.exists(paths.RA_AGENT_PEM)
        else:
            has_ra_cert = False

        if not ra_only:
            if promote:
                # Setup Database
                self.step("creating certificate server db", self.__create_ds_db)
                self.step("setting up initial replication", self.__setup_replication)
                self.step("creating ACIs for admin", self.add_ipaca_aci)
                self.step("creating installation admin user", self.setup_admin)
            self.step("configuring certificate server instance",
                      self.__spawn_instance)
            self.step("Add ipa-pki-wait-running", self.add_ipa_wait)
            self.step("reindex attributes", self.reindex_task)
            self.step("exporting Dogtag certificate store pin",
                      self.create_certstore_passwdfile)
            self.step("stopping certificate server instance to update CS.cfg",
                      self.stop_instance)
            self.step("backing up CS.cfg", self.safe_backup_config)
            self.step("disabling nonces", self.__disable_nonce)
            self.step("set up CRL publishing", self.__enable_crl_publish)
            self.step("enable PKIX certificate path discovery and validation",
                      self.enable_pkix)
            if promote:
                self.step("destroying installation admin user",
                          self.teardown_admin)
            self.step("starting certificate server instance",
                      self.start_instance)
            if promote:
                self.step("Finalize replication settings",
                          self.finalize_replica_config)
        # Step 1 of external is getting a CSR so we don't need to do these
        # steps until we get a cert back from the external CA.
        if self.external != 1:
            if not has_ra_cert:
                self.step("configure certmonger for renewals",
                          self.configure_certmonger_renewal)
                if not self.clone:
                    self.step("requesting RA certificate from CA", self.__request_ra_certificate)
                elif promote:
                    self.step("Importing RA key", self.__import_ra_key)
                else:
                    self.step("importing RA certificate from PKCS #12 file",
                              self.__import_ra_cert)

            if not ra_only:
                self.step("setting audit signing renewal to 2 years", self.set_audit_renewal)
                self.step("restarting certificate server", self.restart_instance)
                if not self.clone:
                    self.step("publishing the CA certificate",
                              self.__export_ca_chain)
                    self.step("adding RA agent as a trusted user", self.__create_ca_agent)
                self.step("authorizing RA to modify profiles", configure_profiles_acl)
                self.step("authorizing RA to manage lightweight CAs",
                          configure_lightweight_ca_acls)
                self.step("Ensure lightweight CAs container exists",
                          ensure_lightweight_cas_container)
                if self.clone and not promote:
                    self.step(
                        "Ensuring backward compatibility",
                        self.__dogtag10_migration)
                self.step("configure certificate renewals", self.configure_renewal)
                self.step("configure Server-Cert certificate renewal", self.track_servercert)
                self.step("Configure HTTP to proxy connections",
                          self.http_proxy)
                self.step("restarting certificate server", self.restart_instance)
                self.step("updating IPA configuration", update_ipa_conf)
                self.step("enabling CA instance", self.__enable_instance)
                if not promote:
                    if self.clone:
                        # DL0 workaround; see docstring of __expose_ca_in_ldap
                        self.step("exposing CA instance on LDAP",
                                  self.__expose_ca_in_ldap)

                    self.step("migrating certificate profiles to LDAP",
                              migrate_profiles_to_ldap)
                    self.step("importing IPA certificate profiles",
                              import_included_profiles)
                    self.step("adding default CA ACL", ensure_default_caacl)
                    self.step("adding 'ipa' CA entry", ensure_ipa_authority_entry)

                self.step("configuring certmonger renewal for lightweight CAs",
                          self.add_lightweight_ca_tracking_requests)

        if ra_only:
            runtime = None
        else:
            runtime = 180

        try:
            self.start_creation(runtime=runtime)
        finally:
            if self.external == 1:
                # Don't remove client DB in external CA step 1
                # https://pagure.io/freeipa/issue/7742
                logger.debug("Keep pkispawn files for step 2")
            else:
                self.clean_pkispawn_files()

    def __spawn_instance(self):
        """
        Create and configure a new CA instance using pkispawn.
        Creates the config file with IPA specific parameters
        and passes it to the base class to call pkispawn
        """

        # Create an empty and secured file
        (cfg_fd, cfg_file) = tempfile.mkstemp()
        os.close(cfg_fd)
        pent = pwd.getpwnam(self.service_user)

        # Create CA configuration
        config = RawConfigParser()
        config.optionxform = str
        config.add_section("CA")

        # Server
        config.set("CA", "pki_security_domain_name", self.security_domain_name)
        config.set("CA", "pki_enable_proxy", "True")
        config.set("CA", "pki_restart_configured_instance", "False")
        config.set("CA", "pki_backup_keys", "True")
        config.set("CA", "pki_backup_password", self.admin_password)
        config.set("CA", "pki_profiles_in_ldap", "True")
        config.set("CA", "pki_default_ocsp_uri",
            "http://{}.{}/ca/ocsp".format(
                ipalib.constants.IPA_CA_RECORD,
                ipautil.format_netloc(api.env.domain)))

        # Configures the status request timeout, i.e. the connect/data
        # timeout on the HTTP request to get the status of Dogtag.
        #
        # This configuration is needed in "multiple IP address" scenarios
        # where this server's hostname has multiple IP addresses but the
        # HTTP server is only listening on one of them.  Without a timeout,
        # if a "wrong" IP address is tried first, it will take a long time
        # to timeout, exceeding the overall timeout hence the request will
        # not be re-tried.  Setting a shorter timeout allows the request
        # to be re-tried.
        #
        # Note that HSMs cause different behaviour so this value might
        # not be suitable for when we implement HSM support.  It is
        # known that a value of 5s is too short in HSM environment.
        #
        config.set("CA", "pki_status_request_timeout", "15")  # 15 seconds

        # Client security database
        config.set("CA", "pki_client_pkcs12_password", self.admin_password)

        # Administrator
        config.set("CA", "pki_admin_name", self.admin_user)
        config.set("CA", "pki_admin_uid", self.admin_user)
        config.set("CA", "pki_admin_email", "root@localhost")
        config.set("CA", "pki_admin_password", self.admin_password)
        config.set("CA", "pki_admin_nickname", "ipa-ca-agent")
        config.set("CA", "pki_admin_subject_dn",
            str(DN(('cn', 'ipa-ca-agent'), self.subject_base)))
        config.set("CA", "pki_client_admin_cert_p12", paths.DOGTAG_ADMIN_P12)

        # Directory server
        config.set("CA", "pki_ds_ldap_port", "389")
        config.set("CA", "pki_ds_password", self.dm_password)
        config.set("CA", "pki_ds_base_dn", six.text_type(self.basedn))
        config.set("CA", "pki_ds_database", "ipaca")

        if self.use_ldaps:
            self._use_ldaps_during_spawn(config)

        # Certificate subject DN's
        config.set("CA", "pki_subsystem_subject_dn",
            str(DN(('cn', 'CA Subsystem'), self.subject_base)))
        config.set("CA", "pki_ocsp_signing_subject_dn",
            str(DN(('cn', 'OCSP Subsystem'), self.subject_base)))
        config.set("CA", "pki_sslserver_subject_dn",
            str(DN(('cn', self.fqdn), self.subject_base)))
        config.set("CA", "pki_audit_signing_subject_dn",
            str(DN(('cn', 'CA Audit'), self.subject_base)))
        config.set(
            "CA", "pki_ca_signing_subject_dn",
            str(self.ca_subject))

        # Certificate nicknames
        config.set("CA", "pki_subsystem_nickname", "subsystemCert cert-pki-ca")
        config.set("CA", "pki_ocsp_signing_nickname", "ocspSigningCert cert-pki-ca")
        config.set("CA", "pki_sslserver_nickname", "Server-Cert cert-pki-ca")
        config.set("CA", "pki_audit_signing_nickname", "auditSigningCert cert-pki-ca")
        config.set("CA", "pki_ca_signing_nickname", "caSigningCert cert-pki-ca")

        # CA key algorithm
        config.set("CA", "pki_ca_signing_key_algorithm", self.ca_signing_algorithm)

        if not (os.path.isdir(paths.PKI_TOMCAT_ALIAS_DIR) and
                os.path.isfile(paths.PKI_TOMCAT_PASSWORD_CONF)):
            # generate pin which we know can be used for FIPS NSS database
            pki_pin = ipautil.ipa_generate_password()
            config.set("CA", "pki_pin", pki_pin)
        else:
            pki_pin = None

        if self.clone:

            if self.no_db_setup:
                config.set("CA", "pki_ds_create_new_db", "False")
                config.set("CA", "pki_clone_setup_replication", "False")
                config.set("CA", "pki_clone_reindex_data", "True")

            cafile = self.pkcs12_info[0]

            # if paths.TMP_CA_P12 exists and is not owned by root,
            # shutil.copy will fail if when fs.protected_regular=1
            # so remove the file first
            remove_file(paths.TMP_CA_P12)
            shutil.copy(cafile, paths.TMP_CA_P12)
            pent = pwd.getpwnam(self.service_user)
            os.chown(paths.TMP_CA_P12, pent.pw_uid, pent.pw_gid)

            # Security domain registration
            config.set("CA", "pki_security_domain_hostname", self.master_host)
            config.set("CA", "pki_security_domain_https_port", "443")
            config.set("CA", "pki_security_domain_user", self.admin_user)
            config.set("CA", "pki_security_domain_password", self.admin_password)

            # Clone
            config.set("CA", "pki_clone", "True")
            config.set("CA", "pki_clone_pkcs12_path", paths.TMP_CA_P12)
            config.set("CA", "pki_clone_pkcs12_password", self.dm_password)
            config.set("CA", "pki_clone_replication_security", "TLS")
            config.set("CA", "pki_clone_replication_master_port", str(self.master_replication_port))
            config.set("CA", "pki_clone_replication_clone_port", "389")
            config.set("CA", "pki_clone_replicate_schema", "False")
            config.set("CA", "pki_clone_uri", "https://%s" % ipautil.format_netloc(self.master_host, 443))

        # External CA
        if self.external == 1:
            config.set("CA", "pki_external", "True")
            config.set("CA", "pki_external_csr_path", self.csr_file)

            if self.ca_type == ExternalCAType.MS_CS.value:
                # Include MS template name extension in the CSR
                template = self.external_ca_profile
                if template is None:
                    # default template name
                    template = MSCSTemplateV1(u"SubCA")

                ext_data = binascii.hexlify(template.get_ext_data())
                config.set("CA", "pki_req_ext_add", "True")
                config.set("CA", "pki_req_ext_oid", template.ext_oid)
                config.set("CA", "pki_req_ext_critical", "False")
                config.set("CA", "pki_req_ext_data", ext_data.decode('ascii'))

        elif self.external == 2:
            cert_file = tempfile.NamedTemporaryFile()
            with open(self.cert_file, 'rb') as f:
                ext_cert = x509.load_unknown_x509_certificate(f.read())
            cert_file.write(ext_cert.public_bytes(x509.Encoding.PEM))
            ipautil.flush_sync(cert_file)

            result = ipautil.run(
                [paths.OPENSSL, 'crl2pkcs7',
                 '-certfile', self.cert_chain_file,
                 '-nocrl'],
                capture_output=True)
            cert_chain = result.output
            # Dogtag chokes on the header and footer, remove them
            # https://bugzilla.redhat.com/show_bug.cgi?id=1127838
            cert_chain = re.search(
                r'(?<=-----BEGIN PKCS7-----).*?(?=-----END PKCS7-----)',
                cert_chain, re.DOTALL).group(0)
            cert_chain_file = ipautil.write_tmp_file(cert_chain)

            config.set("CA", "pki_external", "True")
            config.set("CA", "pki_external_ca_cert_path", cert_file.name)
            config.set("CA", "pki_external_ca_cert_chain_path", cert_chain_file.name)
            config.set("CA", "pki_external_step_two", "True")

        # Generate configuration file
        with open(cfg_file, "w") as f:
            config.write(f)

        # Finally chown the config file (rhbz#1677027)
        os.chown(cfg_file, pent.pw_uid, pent.pw_gid)

        self.backup_state('installed', True)
        try:
            DogtagInstance.spawn_instance(
                self, cfg_file,
                nolog_list=(self.dm_password,
                            self.admin_password,
                            pki_pin)
            )
        finally:
            os.remove(cfg_file)

        if self.external == 1:
            print("The next step is to get %s signed by your CA and re-run %s as:" % (self.csr_file, sys.argv[0]))
            print("%s --external-cert-file=/path/to/signed_certificate --external-cert-file=/path/to/external_ca_certificate" % sys.argv[0])
            sys.exit(0)
        else:
            shutil.move(paths.CA_BACKUP_KEYS_P12,
                        paths.CACERT_P12)

        logger.debug("completed creating ca instance")

    def add_ipa_wait(self):
        """Add ipa-pki-wait-running to pki-tomcatd service
        """
        conf = paths.SYSTEMD_PKI_TOMCAT_IPA_CONF
        directory = os.path.dirname(conf)
        if not os.path.isdir(directory):
            os.mkdir(directory)
        with open(conf, 'w') as f:
            f.write('[Service]\n')
            f.write('ExecStartPost={}\n'.format(paths.IPA_PKI_WAIT_RUNNING))
        tasks.systemd_daemon_reload()

    def safe_backup_config(self):
        """
        Safely handle exceptions if backup_config fails

        The parent class raises an exception if the configuration
        cannot be backed up. Catch that and log the message but
        don't stop the current installer.
        """
        try:
            super(CAInstance, self).backup_config()
        except Exception as e:
            logger.warning("Failed to backup CS.cfg: %s", e)

    def create_certstore_passwdfile(self):
        """
        This method creates a 'pwdfile.txt' file in the Dogtag certificate
        store so that this file can be assumed and used for NSSDatabase/CertDB
        operations in 'certutil' calls.
        """
        passwd = None
        token = 'internal'
        with open(paths.PKI_TOMCAT_PASSWORD_CONF, 'r') as f:
            for line in f:
                (tok, pin) = line.split('=', 1)
                if token == tok:
                    passwd = pin.strip()
                    break
            else:
                raise RuntimeError(
                    "The password to the 'internal' token of the Dogtag "
                    "certificate store was not found.")
        db = certs.CertDB(self.realm, nssdir=paths.PKI_TOMCAT_ALIAS_DIR)
        db.create_passwd_file(passwd)

    def __update_topology(self):
        ld = ldapupdate.LDAPUpdate(ldapi=True, sub_dict={
            'SUFFIX': api.env.basedn,
            'FQDN': self.fqdn,
        })
        ld.update([paths.CA_TOPOLOGY_ULDIF])

    def __disable_nonce(self):
        # Turn off Nonces
        update_result = installutils.update_file(
            self.config, 'ca.enableNonces=true',
            'ca.enableNonces=false')
        if update_result != 0:
            raise RuntimeError("Disabling nonces failed")
        pent = pwd.getpwnam(self.service_user)
        os.chown(self.config, pent.pw_uid, pent.pw_gid)

    def enable_pkix(self):
        directivesetter.set_directive(paths.SYSCONFIG_PKI_TOMCAT,
                                   'NSS_ENABLE_PKIX_VERIFY', '1',
                                   quotes=False, separator='=')

    def __import_ra_cert(self):
        """
        Helper method for IPA domain level 0 replica install
        """
        self.import_ra_cert(self.ra_p12, self.dm_password)

    def import_ra_cert(self, rafile, password=''):
        """
        Cloned RAs will use the same RA agent cert as the master so we
        need to import from a PKCS#12 file.

        Used when setting up replication
        """
        with ipautil.write_tmp_file(password + '\n') as f:
            pwdarg = 'file:{file}'.format(file=f.name)
            # get the private key from the file
            ipautil.run([paths.OPENSSL,
                         "pkcs12",
                         "-in", rafile,
                         "-nocerts", "-nodes",
                         "-out", paths.RA_AGENT_KEY,
                         "-passin", pwdarg])

            # get the certificate from the pkcs12 file
            ipautil.run([paths.OPENSSL,
                         "pkcs12",
                         "-in", rafile,
                         "-clcerts", "-nokeys",
                         "-out", paths.RA_AGENT_PEM,
                         "-passin", pwdarg])
        self.__set_ra_cert_perms()

        self.configure_agent_renewal()

    def __import_ra_key(self):
        self._custodia.import_ra_key()
        self.__set_ra_cert_perms()

        self.configure_agent_renewal()

    def __set_ra_cert_perms(self):
        """
        Sets the correct permissions for the RA_AGENT_PEM, RA_AGENT_KEY files
        """
        ipaapi_gid = grp.getgrnam(ipalib.constants.IPAAPI_GROUP).gr_gid
        for fname in (paths.RA_AGENT_PEM, paths.RA_AGENT_KEY):
            os.chown(fname, -1, ipaapi_gid)
            os.chmod(fname, 0o440)
            tasks.restore_context(fname)

    def __create_ca_agent(self):
        """
        Create CA agent, assign a certificate, and add the user to
        the appropriate groups for accessing CA services.
        """

        # connect to CA database
        conn = ldap2.ldap2(api)
        conn.connect(autobind=True)

        # create ipara user with RA certificate
        user_dn = DN(('uid', "ipara"), ('ou', 'People'), self.basedn)
        entry = conn.make_entry(
            user_dn,
            objectClass=['top', 'person', 'organizationalPerson',
                         'inetOrgPerson', 'cmsuser'],
            uid=["ipara"],
            sn=["ipara"],
            cn=["ipara"],
            usertype=["agentType"],
            userstate=["1"],
            userCertificate=[self.ra_cert],
            description=['2;%s;%s;%s' % (
                self.ra_cert.serial_number,
                DN(self.ca_subject),
                DN(('CN', 'IPA RA'), self.subject_base))])
        conn.add_entry(entry)

        # add ipara user to Certificate Manager Agents group
        group_dn = DN(('cn', 'Certificate Manager Agents'), ('ou', 'groups'),
            self.basedn)
        conn.add_entry_to_group(user_dn, group_dn, 'uniqueMember')

        # add ipara user to Registration Manager Agents group
        group_dn = DN(('cn', 'Registration Manager Agents'), ('ou', 'groups'),
            self.basedn)
        conn.add_entry_to_group(user_dn, group_dn, 'uniqueMember')

        conn.disconnect()

    def __get_ca_chain(self):
        try:
            return dogtag.get_ca_certchain(ca_host=self.fqdn)
        except Exception as e:
            raise RuntimeError("Unable to retrieve CA chain: %s" % str(e))

    def __export_ca_chain(self):
        """
        Get the CA chain from Dogtag NSS DB and write it to paths.IPA_CA_CRT
        """
        # Getting Dogtag CA chain
        chain = self.__get_ca_chain()

        # Convert to DER because the chain comes back as one long string which
        # makes openssl throw up.
        data = base64.b64decode(chain)

        # Get list of PEM certificates
        certlist = x509.pkcs7_to_certs(data, x509.DER)

        # We need to append the certs to the existing file, so start by
        # reading the file
        if os.path.isfile(paths.IPA_CA_CRT):
            ca_certs = x509.load_certificate_list_from_file(paths.IPA_CA_CRT)
            certlist.extend(ca_certs)

        # We have all the certificates in certlist, write them to a PEM file
        for path in [paths.IPA_CA_CRT,
                     paths.KDC_CA_BUNDLE_PEM,
                     paths.CA_BUNDLE_PEM]:
            x509.write_certificate_list(certlist, path, mode=0o644)

    def __request_ra_certificate(self):
        """
        Request the IPA RA certificate from dogtag.

        dogtag automatically generates an admin certificate that
        in a usual deployment would be used in the UI to handle
        administrative duties. IPA does not use this certificate
        except as a bootstrap to generate the RA.

        To do this it bends over backwards a bit by modifying the
        way typical certificates are retrieved using certmonger by
        forcing it to call dogtag-submit directly.
        """

        # create a temp PEM file storing the CA chain
        chain_file = tempfile.NamedTemporaryFile(
            mode="w", dir=paths.VAR_LIB_IPA, delete=False)
        chain_file.close()

        chain = self.__get_ca_chain()
        data = base64.b64decode(chain)
        ipautil.run(
            [paths.OPENSSL,
             "pkcs7",
             "-inform",
             "DER",
             "-print_certs",
             "-out", chain_file.name,
             ], stdin=data, capture_output=False)

        # CA agent cert in PEM form
        agent_cert = tempfile.NamedTemporaryFile(
            mode="w", dir=paths.VAR_LIB_IPA, delete=False)
        agent_cert.close()

        # CA agent key in PEM form
        agent_key = tempfile.NamedTemporaryFile(
            mode="w", dir=paths.VAR_LIB_IPA, delete=False)
        agent_key.close()

        certs.install_pem_from_p12(paths.DOGTAG_ADMIN_P12,
                                   self.dm_password,
                                   agent_cert.name)
        certs.install_key_from_p12(paths.DOGTAG_ADMIN_P12,
                                   self.dm_password,
                                   agent_key.name)

        agent_args = [paths.CERTMONGER_DOGTAG_SUBMIT,
                      "--cafile", chain_file.name,
                      "--ee-url", 'http://%s:8080/ca/ee/ca/' % self.fqdn,
                      "--agent-url",
                      'https://%s:8443/ca/agent/ca/' % self.fqdn,
                      "--certfile", agent_cert.name,
                      "--keyfile", agent_key.name, ]

        helper = " ".join(agent_args)

        # configure certmonger renew agent to use temporary agent cert
        old_helper = certmonger.modify_ca_helper(
            ipalib.constants.RENEWAL_CA_NAME, helper)

        try:
            # The certificate must be requested using caServerCert profile
            # because this profile does not require agent authentication
            reqId = certmonger.request_and_wait_for_cert(
                certpath=(paths.RA_AGENT_PEM, paths.RA_AGENT_KEY),
                principal='host/%s' % self.fqdn,
                subject=str(DN(('CN', 'IPA RA'), self.subject_base)),
                ca=ipalib.constants.RENEWAL_CA_NAME,
                profile='caServerCert',
                pre_command='renew_ra_cert_pre',
                post_command='renew_ra_cert',
                storage="FILE",
                resubmit_timeout=api.env.replication_wait_timeout
            )
            self.__set_ra_cert_perms()

            self.requestId = str(reqId)
            self.ra_cert = x509.load_certificate_from_file(
                paths.RA_AGENT_PEM)
        finally:
            # we can restore the helper parameters
            certmonger.modify_ca_helper(
                ipalib.constants.RENEWAL_CA_NAME, old_helper)
            # remove any temporary files
            for f in (chain_file, agent_cert, agent_key):
                try:
                    os.remove(f.name)
                except OSError:
                    pass

    def prepare_crl_publish_dir(self):
        """
        Prepare target directory for CRL publishing

        Returns a path to the CRL publishing directory
        """
        publishdir = paths.PKI_CA_PUBLISH_DIR

        if not os.path.exists(publishdir):
            os.mkdir(publishdir)

        os.chmod(publishdir, 0o775)
        pent = pwd.getpwnam(self.service_user)
        os.chown(publishdir, 0, pent.pw_gid)

        tasks.restore_context(publishdir)

        return publishdir


    def __enable_crl_publish(self):
        """
        Enable file-based CRL publishing and disable LDAP publishing.

        https://access.redhat.com/knowledge/docs/en-US/Red_Hat_Certificate_System/8.0/html/Admin_Guide/Setting_up_Publishing.html
        """
        with directivesetter.DirectiveSetter(
                self.config, quotes=False, separator='=') as ds:
            # Enable file publishing, disable LDAP
            ds.set('ca.publish.enable', 'true')
            ds.set('ca.publish.ldappublish.enable', 'false')

            # Create the file publisher, der only, not b64
            ds.set(
                'ca.publish.publisher.impl.FileBasedPublisher.class',
                'com.netscape.cms.publish.publishers.FileBasedPublisher'
            )
            prefix = 'ca.publish.publisher.instance.FileBaseCRLPublisher.'
            ds.set(prefix + 'crlLinkExt', 'bin')
            ds.set(prefix + 'directory', self.prepare_crl_publish_dir())
            ds.set(prefix + 'latestCrlLink', 'true')
            ds.set(prefix + 'pluginName', 'FileBasedPublisher')
            ds.set(prefix + 'timeStamp', 'LocalTime')
            ds.set(prefix + 'zipCRLs', 'false')
            ds.set(prefix + 'zipLevel', '9')
            ds.set(prefix + 'Filename.b64', 'false')
            ds.set(prefix + 'Filename.der', 'true')

            # The publishing rule
            ds.set('ca.publish.rule.instance.FileCrlRule.enable', 'true')
            ds.set('ca.publish.rule.instance.FileCrlRule.mapper', 'NoMap')
            ds.set('ca.publish.rule.instance.FileCrlRule.pluginName', 'Rule')
            ds.set('ca.publish.rule.instance.FileCrlRule.predicate', '')
            ds.set(
                'ca.publish.rule.instance.FileCrlRule.publisher',
                'FileBaseCRLPublisher'
            )
            ds.set('ca.publish.rule.instance.FileCrlRule.type', 'crl')

            # Now disable LDAP publishing
            ds.set('ca.publish.rule.instance.LdapCaCertRule.enable', 'false')
            ds.set('ca.publish.rule.instance.LdapCrlRule.enable', 'false')
            ds.set(
                'ca.publish.rule.instance.LdapUserCertRule.enable',
                'false'
            )
            ds.set('ca.publish.rule.instance.LdapXCertRule.enable', 'false')

            # If we are the initial master then we are the CRL generator,
            # otherwise we point to that master for CRLs.
            if not self.clone:
                # These next two are defaults, but I want to be explicit
                # that the initial master is the CRL generator.
                ds.set('ca.crl.MasterCRL.enableCRLCache', 'true')
                ds.set('ca.crl.MasterCRL.enableCRLUpdates', 'true')
                ds.set('ca.listenToCloneModifications', 'true')
            else:
                ds.set('ca.crl.MasterCRL.enableCRLCache', 'false')
                ds.set('ca.crl.MasterCRL.enableCRLUpdates', 'false')
                ds.set('ca.listenToCloneModifications', 'false')

    def uninstall(self):
        # just eat state
        self.restore_state("enabled")

        DogtagInstance.uninstall(self)

        self.restore_state("installed")

        # At one time we removed this user on uninstall. That can potentially
        # orphan files, or worse, if another useradd runs in the interim,
        # cause files to have a new owner.
        self.restore_state("user_exists")

        services.knownservices.dbus.start()
        cmonger = services.knownservices.certmonger
        cmonger.start()

        bus = dbus.SystemBus()
        obj = bus.get_object('org.fedorahosted.certmonger',
                             '/org/fedorahosted/certmonger')
        iface = dbus.Interface(obj, 'org.fedorahosted.certmonger')
        for suffix in ['', '-reuse']:
            name = 'dogtag-ipa-ca-renew-agent' + suffix
            path = iface.find_ca_by_nickname(name)
            if path:
                iface.remove_known_ca(path)

        cmonger.stop()

        # remove ipa-pki-wait-running config
        remove_file(paths.SYSTEMD_PKI_TOMCAT_IPA_CONF)
        try:
            os.rmdir(os.path.dirname(paths.SYSTEMD_PKI_TOMCAT_IPA_CONF))
        except OSError:
            pass
        tasks.systemd_daemon_reload()

        # remove CRL files
        logger.debug("Remove old CRL files")
        try:
            for f in get_crl_files():
                logger.debug("Remove %s", f)
                installutils.remove_file(f)
        except OSError as e:
            logger.warning("Error while removing old CRL files: %s", e)

        # remove CRL directory
        logger.debug("Remove CRL directory")
        if os.path.exists(paths.PKI_CA_PUBLISH_DIR):
            try:
                shutil.rmtree(paths.PKI_CA_PUBLISH_DIR)
            except OSError as e:
                logger.warning("Error while removing CRL publish "
                               "directory: %s", e)

    def unconfigure_certmonger_renewal_guard(self):
        if not self.is_configured():
            return

        helper = self.restore_state('certmonger_dogtag_helper')
        if helper:
            bus = dbus.SystemBus()
            obj = bus.get_object('org.fedorahosted.certmonger',
                                 '/org/fedorahosted/certmonger')
            iface = dbus.Interface(obj, 'org.fedorahosted.certmonger')
            path = iface.find_ca_by_nickname('dogtag-ipa-renew-agent')
            if path:
                ca_obj = bus.get_object('org.fedorahosted.certmonger', path)
                ca_iface = dbus.Interface(ca_obj,
                                          'org.freedesktop.DBus.Properties')
                ca_iface.Set('org.fedorahosted.certmonger.ca',
                             'external-helper', helper)

    def configure_agent_renewal(self):
        try:
            certmonger.start_tracking(
                certpath=(paths.RA_AGENT_PEM, paths.RA_AGENT_KEY),
                ca='dogtag-ipa-ca-renew-agent',
                pre_command='renew_ra_cert_pre',
                post_command='renew_ra_cert',
                storage='FILE')
        except RuntimeError as e:
            logger.error(
                "certmonger failed to start tracking certificate: %s", e)

    def stop_tracking_certificates(self):
        """Stop tracking our certificates. Called on uninstall.
        """
        super(CAInstance, self).stop_tracking_certificates(False)

        # stop tracking lightweight CA signing certs
        for request_id in certmonger.get_requests_for_dir(self.nss_db):
            nickname = certmonger.get_request_value(request_id, 'key-nickname')
            if nickname.startswith('caSigningCert cert-pki-ca '):
                certmonger.stop_tracking(self.nss_db, nickname=nickname)

        try:
            certmonger.stop_tracking(certfile=paths.RA_AGENT_PEM)
        except RuntimeError as e:
            logger.error(
                "certmonger failed to stop tracking certificate: %s", e)

        services.knownservices.certmonger.stop()


    def set_audit_renewal(self):
        """
        The default renewal time for the audit signing certificate is
        six months rather than two years. Fix it. This is BZ 843979.
        """
        # Check the default validity period of the audit signing cert
        # and set it to 2 years if it is 6 months.
        cert_range = directivesetter.get_directive(
            paths.CASIGNEDLOGCERT_CFG,
            'policyset.caLogSigningSet.2.default.params.range',
            separator='='
        )
        logger.debug(
            'caSignedLogCert.cfg profile validity range is %s', cert_range)
        if cert_range == "180":
            directivesetter.set_directive(
                paths.CASIGNEDLOGCERT_CFG,
                'policyset.caLogSigningSet.2.default.params.range',
                '720',
                quotes=False,
                separator='='
            )
            directivesetter.set_directive(
                paths.CASIGNEDLOGCERT_CFG,
                'policyset.caLogSigningSet.2.constraint.params.range',
                '720',
                quotes=False,
                separator='='
            )
            logger.debug(
                'updated caSignedLogCert.cfg profile validity range to 720')
            return True
        return False

    def is_renewal_master(self, fqdn=None):
        if fqdn is None:
            fqdn = api.env.host

        dn = DN(('cn', 'CA'), ('cn', fqdn), api.env.container_masters,
                api.env.basedn)
        renewal_filter = '(ipaConfigString=caRenewalMaster)'
        try:
            api.Backend.ldap2.get_entries(base_dn=dn, filter=renewal_filter,
                                          attrs_list=[])
        except errors.NotFound:
            return False

        return True

    def set_renewal_master(self, fqdn=None):
        if fqdn is None:
            fqdn = api.env.host

        base_dn = DN(api.env.container_masters, api.env.basedn)
        filter = '(&(cn=CA)(ipaConfigString=caRenewalMaster))'
        try:
            entries = api.Backend.ldap2.get_entries(
                base_dn=base_dn, filter=filter, attrs_list=['ipaConfigString'])
        except errors.NotFound:
            entries = []

        dn = DN(('cn', 'CA'), ('cn', fqdn), base_dn)
        master_entry = api.Backend.ldap2.get_entry(dn, ['ipaConfigString'])

        for entry in entries:
            if master_entry is not None and entry.dn == master_entry.dn:
                master_entry = None
                continue

            entry['ipaConfigString'] = [x for x in entry['ipaConfigString']
                                        if x.lower() != 'carenewalmaster']
            api.Backend.ldap2.update_entry(entry)

        if master_entry is not None:
            master_entry['ipaConfigString'].append('caRenewalMaster')
            api.Backend.ldap2.update_entry(master_entry)

    def update_cert_config(self, nickname, cert):
        """
        When renewing a CA subsystem certificate the configuration file
        needs to get the new certificate as well.

        nickname is one of the known nicknames.
        cert is a DER-encoded certificate.
        """

        # The cert directive to update per nickname
        directives = {'auditSigningCert cert-pki-ca': 'ca.audit_signing.cert',
                      'ocspSigningCert cert-pki-ca': 'ca.ocsp_signing.cert',
                      'caSigningCert cert-pki-ca': 'ca.signing.cert',
                      'subsystemCert cert-pki-ca': 'ca.subsystem.cert',
                      'Server-Cert cert-pki-ca': 'ca.sslserver.cert'}

        try:
            self.backup_config()
        except Exception as e:
            syslog.syslog(syslog.LOG_ERR, "Failed to backup CS.cfg: %s" % e)

        if nickname in directives:
            super(CAInstance, self).update_cert_cs_cfg(
                directives[nickname], cert)

    def __create_ds_db(self):
        '''
        Create PKI database. Is needed when pkispawn option
        pki_ds_create_new_db is set to False
        '''

        backend = 'ipaca'
        suffix = DN(('o', 'ipaca'))

        # replication
        dn = DN(('cn', str(suffix)), ('cn', 'mapping tree'), ('cn', 'config'))
        entry = api.Backend.ldap2.make_entry(
            dn,
            objectclass=["top", "extensibleObject", "nsMappingTree"],
            cn=[suffix],
        )
        entry['nsslapd-state'] = ['Backend']
        entry['nsslapd-backend'] = [backend]
        api.Backend.ldap2.add_entry(entry)

        # database
        dn = DN(('cn', 'ipaca'), ('cn', 'ldbm database'), ('cn', 'plugins'),
                ('cn', 'config'))
        entry = api.Backend.ldap2.make_entry(
            dn,
            objectclass=["top", "extensibleObject", "nsBackendInstance"],
            cn=[backend],
        )
        entry['nsslapd-suffix'] = [suffix]
        api.Backend.ldap2.add_entry(entry)

    def __setup_replication(self):
        repl = replication.CAReplicationManager(self.realm, self.fqdn)
        repl.setup_cs_replication(self.master_host)

        # Activate Topology for o=ipaca segments
        self.__update_topology()

    def finalize_replica_config(self):
        repl = replication.CAReplicationManager(self.realm, self.fqdn)
        repl.finalize_replica_config(self.master_host)

    def __enable_instance(self):
        basedn = ipautil.realm_to_suffix(self.realm)
        if not self.clone:
            config = ['caRenewalMaster']
        else:
            config = []
        self.ldap_configure('CA', self.fqdn, None, basedn, config)

    def __expose_ca_in_ldap(self):
        """
        In a case when replica is created on DL0 we need to make
        sure that query for CA service record of this replica in
        ldap will succeed in time of installation.
        This method is needed for sucessfull replica installation
        on DL0 and should be removed alongside with code for DL0.

        To suppress deprecation warning message this method is
        not invoking ldap_enable() but _ldap_enable() method.
        """

        basedn = ipautil.realm_to_suffix(self.realm)
        if not self.clone:
            config = ['caRenewalMaster']
        else:
            config = []
        self._ldap_enable(ENABLED_SERVICE, "CA", self.fqdn, basedn, config)

    def setup_lightweight_ca_key_retrieval(self):
        # Important: there is a typo in the below string, which is known
        # and should not be fixed as existing installations already use it
        LWCA_KEY_RETRIEVAL = 'setup_lwca_key_retieval'
        if sysupgrade.get_upgrade_state('dogtag', LWCA_KEY_RETRIEVAL):
            return

        logger.debug('Set up lightweight CA key retrieval')

        self.__setup_lightweight_ca_key_retrieval_kerberos()
        self.__setup_lightweight_ca_key_retrieval_custodia()

        logger.debug('Configuring key retriever')
        directives = [
            ('features.authority.keyRetrieverClass',
                'com.netscape.ca.ExternalProcessKeyRetriever'),
            ('features.authority.keyRetrieverConfig.executable',
                paths.IPA_PKI_RETRIEVE_KEY),
        ]
        for k, v in directives:
            directivesetter.set_directive(
                self.config, k, v, quotes=False, separator='=')

        sysupgrade.set_upgrade_state('dogtag', LWCA_KEY_RETRIEVAL, True)

    def __setup_lightweight_ca_key_retrieval_kerberos(self):
        pent = pwd.getpwnam(self.service_user)

        logger.debug('Creating principal')
        installutils.kadmin_addprinc(self.principal)
        self.suffix = ipautil.realm_to_suffix(self.realm)
        self.move_service(self.principal)

        logger.debug('Retrieving keytab')
        installutils.create_keytab(self.keytab, self.principal)
        os.chmod(self.keytab, 0o600)
        os.chown(self.keytab, pent.pw_uid, pent.pw_gid)

    def __setup_lightweight_ca_key_retrieval_custodia(self):
        pent = pwd.getpwnam(self.service_user)

        logger.debug('Creating Custodia keys')
        custodia_basedn = DN(
            ('cn', 'custodia'), ('cn', 'ipa'), ('cn', 'etc'), api.env.basedn)
        ensure_entry(
            custodia_basedn,
            objectclass=['top', 'nsContainer'],
            cn=['custodia'],
        )
        ensure_entry(
            DN(('cn', 'dogtag'), custodia_basedn),
            objectclass=['top', 'nsContainer'],
            cn=['dogtag'],
        )
        keyfile = os.path.join(paths.PKI_TOMCAT, self.service_prefix + '.keys')
        keystore = IPAKEMKeys({'server_keys': keyfile})
        keystore.generate_keys(self.service_prefix)
        os.chmod(keyfile, 0o600)
        os.chown(keyfile, pent.pw_uid, pent.pw_gid)

    def __remove_lightweight_ca_key_retrieval_custodia(self):
        keyfile = os.path.join(paths.PKI_TOMCAT,
                               self.service_prefix + '.keys')
        keystore = IPAKEMKeys({'server_keys': keyfile})
        # Call remove_server_keys_file explicitly to ensure that the key
        # file is always removed.
        keystore.remove_server_keys_file()
        try:
            keystore.remove_keys(self.service_prefix)
        except (ldap.CONNECT_ERROR, ldap.SERVER_DOWN):
            logger.debug(
                "Cannot remove custodia keys now, server_del takes care of "
                "them later."
            )

    def add_lightweight_ca_tracking_requests(self):
        try:
            lwcas = api.Backend.ldap2.get_entries(
                base_dn=api.env.basedn,
                filter='(objectclass=ipaca)',
                attrs_list=['cn', 'ipacaid'],
            )
            add_lightweight_ca_tracking_requests(lwcas)
        except errors.NotFound:
            # shouldn't happen, but don't fail if it does
            logger.warning(
                "Did not find any lightweight CAs; nothing to track")

    def __dogtag10_migration(self):
        ld = ldapupdate.LDAPUpdate(ldapi=True, sub_dict={
            'SUFFIX': api.env.basedn,
            'FQDN': self.fqdn,
        })
        ld.update([os.path.join(paths.UPDATES_DIR,
                                '50-dogtag10-migration.update')]
                  )

    def is_crlgen_enabled(self):
        """Check if the local CA instance is generating CRL

        Three conditions must be met to consider that the local CA is CRL
        generation master:
        - in CS.cfg ca.crl.MasterCRL.enableCRLCache=true
        - in CS.cfg ca.crl.MasterCRL.enableCRLUpdates=true
        - in /etc/httpd/conf.d/ipa-pki-proxy.conf the RewriteRule
        ^/ipa/crl/MasterCRL.bin is disabled (commented or removed)

        If the values are inconsistent, an exception is raised
        :returns: True/False
        :raises: InconsistentCRLGenConfigException if the config is
                 inconsistent
        """
        try:
            cache = directivesetter.get_directive(
                self.config, 'ca.crl.MasterCRL.enableCRLCache', '=')
            enableCRLCache = cache.lower() == 'true'
            updates = directivesetter.get_directive(
                self.config, 'ca.crl.MasterCRL.enableCRLUpdates', '=')
            enableCRLUpdates = updates.lower() == 'true'

            # If the values are different, the config is inconsistent
            if enableCRLCache != enableCRLUpdates:
                raise InconsistentCRLGenConfigException(
                    "Configuration is inconsistent, please check "
                    "ca.crl.MasterCRL.enableCRLCache and "
                    "ca.crl.MasterCRL.enableCRLUpdates in {} and "
                    "run ipa-crlgen-manage [enable|disable] to repair".format(
                        self.config))
        except IOError:
            raise RuntimeError(
                "Unable to read {}".format(self.config))

        # At this point enableCRLCache and enableCRLUpdates have the same value
        try:
            rewriteRuleDisabled = True
            p = re.compile(self.crl_rewrite_pattern)
            with open(paths.HTTPD_IPA_PKI_PROXY_CONF) as f:
                for line in f.readlines():
                    if p.search(line):
                        rewriteRuleDisabled = False
                        break
        except IOError:
            raise RuntimeError(
                "Unable to read {}".format(paths.HTTPD_IPA_PKI_PROXY_CONF))

        # if enableCRLUpdates and rewriteRuleDisabled are different, the config
        # is inconsistent
        if enableCRLUpdates != rewriteRuleDisabled:
            raise InconsistentCRLGenConfigException(
                "Configuration is inconsistent, please check "
                "ca.crl.MasterCRL.enableCRLCache in {} and the "
                "RewriteRule ^/ipa/crl/MasterCRL.bin in {} and "
                "run ipa-crlgen-manage [enable|disable] to repair".format(
                    self.config, paths.HTTPD_IPA_PKI_PROXY_CONF))
        return enableCRLUpdates

    def setup_crlgen(self, setup_crlgen):
        """Configure the local host for CRL generation

        :param setup_crlgen: if True enable CRL generation, if False, disable
        """
        try:
            crlgen_enabled = self.is_crlgen_enabled()
            if crlgen_enabled == setup_crlgen:
                logger.info(
                    "Nothing to do, CRL generation already %s",
                    "enabled" if crlgen_enabled else "disabled")
                return
        except InconsistentCRLGenConfigException:
            logger.warning("CRL generation is partially enabled, repairing...")

        # Stop PKI
        logger.info("Stopping %s", self.service_name)
        self.stop_instance()
        logger.debug("%s successfully stopped", self.service_name)

        # Edit the CS.cfg directives
        logger.info("Editing %s", self.config)
        with directivesetter.DirectiveSetter(
                self.config, quotes=False, separator='=') as ds:
            # Convert the bool setup_crlgen to a lowercase string
            str_value = str(setup_crlgen).lower()
            ds.set('ca.crl.MasterCRL.enableCRLCache', str_value)
            ds.set('ca.crl.MasterCRL.enableCRLUpdates', str_value)

        # Start pki-tomcat
        logger.info("Starting %s", self.service_name)
        self.start_instance()
        logger.debug("%s successfully started", self.service_name)

        # Edit the RewriteRule
        def comment_rewriterule():
            logger.info("Editing %s", paths.HTTPD_IPA_PKI_PROXY_CONF)
            # look for the pattern RewriteRule ^/ipa/crl/MasterCRL.bin ..
            # and comment out
            p = re.compile(self.crl_rewrite_pattern, re.MULTILINE)
            with open(paths.HTTPD_IPA_PKI_PROXY_CONF) as f:
                content = f.read()
            new_content = p.sub(r"#\1", content)
            with open(paths.HTTPD_IPA_PKI_PROXY_CONF, 'w') as f:
                f.write(new_content)

        def uncomment_rewriterule():
            logger.info("Editing %s", paths.HTTPD_IPA_PKI_PROXY_CONF)
            # check if the pattern RewriteRule ^/ipa/crl/MasterCRL.bin ..
            # is already present
            present = False
            p = re.compile(self.crl_rewrite_pattern, re.MULTILINE)
            with open(paths.HTTPD_IPA_PKI_PROXY_CONF) as f:
                content = f.read()
            present = p.search(content)
            # Remove the comment
            p_comment = re.compile(self.crl_rewrite_comment, re.MULTILINE)
            new_content = p_comment.sub("", content)
            # If not already present, add RewriteRule
            if not present:
                new_content += self.crl_rewriterule.format(api.env.host)
            # Finally write the file
            with open(paths.HTTPD_IPA_PKI_PROXY_CONF, 'w') as f:
                f.write(new_content)

        try:
            if setup_crlgen:
                comment_rewriterule()
            else:
                uncomment_rewriterule()

        except IOError:
            raise RuntimeError(
                "Unable to access {}".format(paths.HTTPD_IPA_PKI_PROXY_CONF))

        # Restart httpd
        http_service = services.knownservices.httpd
        logger.info("Restarting %s", http_service.service_name)
        http_service.restart()
        logger.debug("%s successfully restarted", http_service.service_name)

        # make sure a CRL is generated if setup_crl is True
        if setup_crlgen:
            logger.info("Forcing CRL update")
            api.Backend.ra.override_port = 8443
            result = api.Backend.ra.updateCRL(wait='true')
            if result.get('crlUpdate', 'Failure') == 'Success':
                logger.debug("Successfully updated CRL")
            api.Backend.ra.override_port = None


def __update_entry_from_cert(make_filter, make_entry, cert):
    """
    Given a certificate and functions to make a filter based on the
    cert, and make a new entry based on the cert, update database
    accordingly.

    :param make_filter:
        function that takes a certificate in DER format and
        returns an LDAP search filter

    :param make_entry:
        function that takes a certificate in DER format and an
        LDAP entry, and returns the new state of the LDAP entry.
        Return the input unchanged to skip an entry.

    :param cert:
        An IPACertificate object

    Logging is done via syslog.

    Return ``True`` if all updates were successful (zero updates is
    vacuously successful) otherwise ``False``.

    """

    base_dn = DN(('o', 'ipaca'))

    attempts = 0
    updated = False

    while attempts < 10:
        conn = None
        try:
            conn = ldap2.ldap2(api)
            conn.connect(autobind=True)

            db_filter = make_filter(cert)
            try:
                entries = conn.get_entries(base_dn, conn.SCOPE_SUBTREE, db_filter)
            except errors.NotFound:
                entries = []

            updated = True

            for entry in entries:
                syslog.syslog(
                    syslog.LOG_NOTICE, 'Updating entry %s' % str(entry.dn))

                try:
                    entry = make_entry(cert, entry)
                    conn.update_entry(entry)
                except errors.EmptyModlist:
                    pass
                except Exception as e:
                    syslog.syslog(
                        syslog.LOG_ERR,
                        'Updating entry %s failed: %s' % (str(entry.dn), e))
                    updated = False

            break
        except errors.NetworkError:
            syslog.syslog(
                syslog.LOG_ERR,
                'Connection to %s failed, sleeping 30s' % api.env.ldap_uri)
            time.sleep(30)
            attempts += 1
        except Exception as e:
            syslog.syslog(syslog.LOG_ERR, 'Caught unhandled exception: %s' % e)
            break
        finally:
            if conn is not None and conn.isconnected():
                conn.disconnect()

    if not updated:
        syslog.syslog(syslog.LOG_ERR, 'Update failed.')
        return False

    return True

def update_people_entry(cert):
    """
    Update the userCerticate for an entry in the dogtag ou=People. This
    is needed when a certificate is renewed.
    """
    def make_filter(cert):
        subject = DN(cert.subject)
        issuer = DN(cert.issuer)
        return ldap2.ldap2.combine_filters(
            [
                ldap2.ldap2.make_filter({'objectClass': 'inetOrgPerson'}),
                ldap2.ldap2.make_filter(
                    {'description': ';%s;%s' % (issuer, subject)},
                    exact=False, trailing_wildcard=False),
            ],
            ldap2.ldap2.MATCH_ALL)

    def make_entry(cert, entry):
        serial_number = cert.serial_number
        subject = DN(cert.subject)
        issuer = DN(cert.issuer)
        entry['usercertificate'].append(cert)
        entry['description'] = '2;%d;%s;%s' % (serial_number, issuer, subject)
        return entry

    return __update_entry_from_cert(make_filter, make_entry, cert)


def update_authority_entry(cert):
    """
    Find the authority entry for the given cert, and update the
    serial number to match the given cert.
    """
    def make_filter(cert):
        subject = str(DN(cert.subject))
        return ldap2.ldap2.make_filter(
            dict(objectclass='authority', authoritydn=subject),
            rules=ldap2.ldap2.MATCH_ALL,
        )

    def make_entry(cert, entry):
        entry['authoritySerial'] = cert.serial_number
        return entry

    return __update_entry_from_cert(make_filter, make_entry, cert)


def ensure_ldap_profiles_container():
    ensure_entry(
        DN(('ou', 'certificateProfiles'), ('ou', 'ca'), ('o', 'ipaca')),
        objectclass=['top', 'organizationalUnit'],
        ou=['certificateProfiles'],
    )

def ensure_lightweight_cas_container():
    return ensure_entry(
        DN(('ou', 'authorities'), ('ou', 'ca'), ('o', 'ipaca')),
        objectclass=['top', 'organizationalUnit'],
        ou=['authorities'],
    )


def ensure_entry(dn, **attrs):
    """Ensure an entry exists.

    If an entry with the given DN already exists, return ``False``,
    otherwise add the entry and return ``True``.

    """
    conn = ldap2.ldap2(api)
    if not conn.isconnected():
        conn.connect(autobind=True)

    try:
        conn.get_entry(dn)
        return False
    except errors.NotFound:
        # entry doesn't exist; add it
        entry = conn.make_entry(dn, **attrs)
        conn.add_entry(entry)
        return True
    finally:
        conn.disconnect()


def configure_profiles_acl():
    """Allow the Certificate Manager Agents group to modify profiles."""
    new_rules = [
        'certServer.profile.configuration:read,modify' +
        ':allow (read,modify) group="Certificate Manager Agents"' +
        ':Certificate Manager agents may modify (create/update/delete) ' +
        'and read profiles',

        'certServer.ca.account:login,logout' +
        ':allow (login,logout) user="anybody"' +
        ':Anybody can login and logout',
    ]
    return __add_acls(new_rules)


def configure_lightweight_ca_acls():
    """Allow Certificate Manager Agents to manage lightweight CAs."""
    new_rules = [
        'certServer.ca.authorities:list,read' +
        ':allow (list,read) user="anybody"' +
        ':Anybody may list and read lightweight authorities',

        'certServer.ca.authorities:create,modify' +
        ':allow (create,modify) group="Administrators"' +
        ':Administrators may create and modify lightweight authorities',

        'certServer.ca.authorities:delete' +
        ':allow (delete) group="Administrators"' +
        ':Administrators may delete lightweight authorities',

        'certServer.ca.authorities:create,modify,delete' +
        ':allow (create,modify,delete) group="Certificate Manager Agents"' +
        ':Certificate Manager Agents may manage lightweight authorities',
    ]
    return __add_acls(new_rules)


def __add_acls(new_rules):
    """Add the given Dogtag ACLs.

    ``new_rules``
        Iterable of ACL rule values to add

    Return ``True`` if any ACLs were added otherwise ``False``.

    """
    updated = False

    dn = DN(('cn', 'aclResources'), ('o', 'ipaca'))

    conn = api.Backend.ldap2
    entry = conn.get_entry(dn)
    cur_rules = entry.get('resourceACLS', [])
    add_rules = [rule for rule in new_rules if rule not in cur_rules]
    if add_rules:
        cur_rules.extend(add_rules)
        conn.update_entry(entry)
        updated = True

    return updated


def __get_profile_config(profile_id):
    sub_dict = dict(
        DOMAIN=ipautil.format_netloc(api.env.domain),
        IPA_CA_RECORD=ipalib.constants.IPA_CA_RECORD,
        CRL_ISSUER='CN=Certificate Authority,o=ipaca',
        SUBJECT_DN_O=dsinstance.DsInstance().find_subject_base(),
    )

    # To work around lack of proper profile upgrade system, we ship
    # two versions of some profiles - one for new installs only, and
    # the other for upgrading to LDAP-based profiles in an existing
    # deployment.
    #
    # Select UPGRADE version if we are in the 'updates' API context
    # and an upgrade-specific version of the profile exists.
    #
    profile_filename = '/usr/share/ipa/profiles/{}.cfg'.format(profile_id)
    profile_upg_filename = \
        '/usr/share/ipa/profiles/{}.UPGRADE.cfg'.format(profile_id)
    if api.env.context == 'updates' and os.path.isfile(profile_upg_filename):
        profile_filename = profile_upg_filename

    return ipautil.template_file(profile_filename, sub_dict)

def import_included_profiles():
    conn = ldap2.ldap2(api)
    if not conn.isconnected():
        conn.connect(autobind=True)

    ensure_entry(
        DN(('cn', 'ca'), api.env.basedn),
        objectclass=['top', 'nsContainer'],
        cn=['ca'],
    )
    ensure_entry(
        DN(api.env.container_certprofile, api.env.basedn),
        objectclass=['top', 'nsContainer'],
        cn=['certprofiles'],
    )

    api.Backend.ra_certprofile.override_port = 8443

    for (profile_id, desc, store_issued) in dogtag.INCLUDED_PROFILES:
        dn = DN(('cn', profile_id),
            api.env.container_certprofile, api.env.basedn)
        try:
            conn.get_entry(dn)
            continue  # the profile is present
        except errors.NotFound:
            # profile not found; add it
            entry = conn.make_entry(
                dn,
                objectclass=['ipacertprofile'],
                cn=[profile_id],
                description=[desc],
                ipacertprofilestoreissued=['TRUE' if store_issued else 'FALSE'],
            )
            conn.add_entry(entry)

            # Create the profile, replacing any existing profile of same name
            profile_data = __get_profile_config(profile_id)
            _create_dogtag_profile(profile_id, profile_data, overwrite=True)
            logger.debug("Imported profile '%s'", profile_id)

    api.Backend.ra_certprofile.override_port = None
    conn.disconnect()


def repair_profile_caIPAserviceCert():
    """
    A regression caused replica installation to replace the FreeIPA
    version of caIPAserviceCert with the version shipped by Dogtag.

    This function detects and repairs occurrences of this problem.

    """
    api.Backend.ra_certprofile.override_port = 8443

    profile_id = 'caIPAserviceCert'

    with api.Backend.ra_certprofile as profile_api:
        try:
            cur_config = profile_api.read_profile(profile_id).splitlines()
        except errors.RemoteRetrieveError:
            # no profile there to check/repair
            api.Backend.ra_certprofile.override_port = None
            return

    indicators = [
        (
            b"policyset.serverCertSet.1.default.params.name="
            b"CN=$request.req_subject_name.cn$, OU=pki-ipa, O=IPA "
        ),
        (
            b"policyset.serverCertSet.9.default.params."
            b"crlDistPointsPointName_0="
            b"https://ipa.example.com/ipa/crl/MasterCRL.bin"
        ),
    ]
    need_repair = all(l in cur_config for l in indicators)

    if need_repair:
        logger.debug(
            "Detected that profile '%s' has been replaced with "
            "incorrect version; begin repair.", profile_id)
        _create_dogtag_profile(
            profile_id, __get_profile_config(profile_id), overwrite=True)
        logger.debug("Repair of profile '%s' complete.", profile_id)

    api.Backend.ra_certprofile.override_port = None


def migrate_profiles_to_ldap():
    """Migrate profiles from filesystem to LDAP.

    This must be run *after* switching to the LDAPProfileSubsystem
    and restarting the CA.

    The profile might already exist, e.g. if a replica was already
    upgraded, so this case is ignored.

    """
    ensure_ldap_profiles_container()
    api.Backend.ra_certprofile.override_port = 8443

    with open(paths.CA_CS_CFG_PATH) as f:
        cs_cfg = f.read()
    match = re.search(r'^profile\.list=(\S*)', cs_cfg, re.MULTILINE)
    profile_ids = match.group(1).split(',')

    for profile_id in profile_ids:
        match = re.search(
            r'^profile\.{}\.config=(\S*)'.format(profile_id),
            cs_cfg, re.MULTILINE
        )
        if match is None:
            logger.info("No file for profile '%s'; skipping", profile_id)
            continue
        filename = match.group(1)

        match = re.search(
            r'^profile\.{}\.class_id=(\S*)'.format(profile_id),
            cs_cfg, re.MULTILINE
        )
        if match is None:
            logger.info("No class_id for profile '%s'; skipping", profile_id)
            continue
        class_id = match.group(1)

        with open(filename) as f:
            profile_data = f.read()
            if profile_data[-1] != '\n':
                profile_data += '\n'
            profile_data += 'profileId={}\n'.format(profile_id)
            profile_data += 'classId={}\n'.format(class_id)

            # Import the profile, but do not replace it if it already exists.
            # This prevents replicas from replacing IPA-managed profiles with
            # Dogtag default profiles of same name.
            #
            _create_dogtag_profile(profile_id, profile_data, overwrite=False)

    api.Backend.ra_certprofile.override_port = None


def _create_dogtag_profile(profile_id, profile_data, overwrite):
    with api.Backend.ra_certprofile as profile_api:
        # import the profile
        try:
            profile_api.create_profile(profile_data)
            logger.debug("Profile '%s' successfully migrated to LDAP",
                         profile_id)
        except errors.RemoteRetrieveError as e:
            logger.debug("Error migrating '%s': %s", profile_id, e)

            # profile already exists
            if overwrite:
                try:
                    profile_api.disable_profile(profile_id)
                except errors.RemoteRetrieveError:
                    logger.debug(
                        "Failed to disable profile '%s' "
                        "(it is probably already disabled)",
                        profile_id)
                profile_api.update_profile(profile_id, profile_data)

        # enable the profile
        try:
            profile_api.enable_profile(profile_id)
        except errors.RemoteRetrieveError:
            logger.debug(
                "Failed to enable profile '%s' "
                "(it is probably already enabled)",
                profile_id)


def ensure_ipa_authority_entry():
    """Add the IPA CA ipaCa object if missing.

    This requires the "host authority" authority entry to have been
    created, which Dogtag will do automatically upon startup, if the
    ou=authorities,ou=ca,o=ipaca container exists.  Therefore, the
    ``ensure_lightweight_cas_container`` function must be executed,
    and Dogtag restarted, before executing this function.

    """

    # find out authority id, issuer DN and subject DN of IPA CA
    api.Backend.ra_lightweight_ca.override_port = 8443
    with api.Backend.ra_lightweight_ca as lwca:
        data = lwca.read_ca('host-authority')
        attrs = dict(
            ipacaid=data['id'],
            ipacaissuerdn=data['issuerDN'],
            ipacasubjectdn=data['dn'],
        )
    api.Backend.ra_lightweight_ca.override_port = None

    ensure_entry(
        DN(api.env.container_ca, api.env.basedn),
        objectclass=['top', 'nsContainer'],
        cn=['cas'],
    )
    ensure_entry(
        DN(('cn', ipalib.constants.IPA_CA_CN), api.env.container_ca, api.env.basedn),
        objectclass=['top', 'ipaca'],
        cn=[ipalib.constants.IPA_CA_CN],
        description=['IPA CA'],
        **attrs
    )


def ensure_default_caacl():
    """Add the default CA ACL if missing."""
    ensure_entry(
        DN(('cn', 'ca'), api.env.basedn),
        objectclass=['top', 'nsContainer'],
        cn=['ca'],
    )
    ensure_entry(
        DN(api.env.container_caacl, api.env.basedn),
        objectclass=['top', 'nsContainer'],
        cn=['certprofiles'],
    )

    if not api.Command.caacl_find()['result']:
        api.Command.caacl_add(u'hosts_services_caIPAserviceCert',
            hostcategory=u'all', servicecategory=u'all')
        api.Command.caacl_add_profile(u'hosts_services_caIPAserviceCert',
            certprofile=(u'caIPAserviceCert',))


def add_lightweight_ca_tracking_requests(lwcas):
    """Add tracking requests for the given lightweight CAs.

    The entries must have the 'cn' and 'ipacaid' attributes.

    The IPA CA, if present, is skipped.

    """
    for entry in lwcas:
        if ipalib.constants.IPA_CA_CN in entry['cn']:
            continue

        nickname = "{} {}".format(
                ipalib.constants.IPA_CA_NICKNAME,
                entry['ipacaid'][0])
        criteria = {
            'cert-database': paths.PKI_TOMCAT_ALIAS_DIR,
            'cert-nickname': nickname,
            'ca-name': ipalib.constants.RENEWAL_CA_NAME,
        }
        request_id = certmonger.get_request_id(criteria)
        if request_id is None:
            try:
                certmonger.start_tracking(
                    certpath=paths.PKI_TOMCAT_ALIAS_DIR,
                    pin=certmonger.get_pin('internal'),
                    nickname=nickname,
                    ca=ipalib.constants.RENEWAL_CA_NAME,
                    profile='caCACert',
                    pre_command='stop_pkicad',
                    post_command='renew_ca_cert "%s"' % nickname,
                )
                logger.debug(
                    'Lightweight CA renewal: '
                    'added tracking request for "%s"', nickname)
            except RuntimeError as e:
                logger.error(
                    'Lightweight CA renewal: Certmonger failed to '
                    'start tracking certificate: %s', e)
        else:
            logger.debug(
                'Lightweight CA renewal: '
                'already tracking certificate "%s"', nickname)


def update_ipa_conf():
    """
    Update IPA configuration file to ensure that RA plugins are enabled and
    that CA host points to localhost
    """
    parser = RawConfigParser()
    parser.read(paths.IPA_DEFAULT_CONF)
    parser.set('global', 'enable_ra', 'True')
    parser.set('global', 'ra_plugin', 'dogtag')
    parser.set('global', 'dogtag_version', '10')
    parser.remove_option('global', 'ca_host')
    with open(paths.IPA_DEFAULT_CONF, 'w') as f:
        parser.write(f)


class ExternalCAProfile(object):
    """
    An external CA profile configuration.  Currently the only
    subclasses are for Microsoft CAs, for providing data in the
    "Certificate Template" extension.

    Constructing this class will actually return an instance of a
    subclass.

    Subclasses MUST set ``valid_for``.

    """
    def __init__(self, s=None):
        self.unparsed_input = s

    # Which external CA types is the data valid for?
    # A set of VALUES of the ExternalCAType enum.
    valid_for = set()

    def __new__(cls, s=None):
        """Construct the ExternalCAProfile value.

        Return an instance of a subclass determined by
        the format of the argument.

        """
        # we are directly constructing a subclass; instantiate
        # it and be done
        if cls is not ExternalCAProfile:
            return super(ExternalCAProfile, cls).__new__(cls)

        # construction via the base class; therefore the string
        # argument is required, and is used to determine which
        # subclass to construct
        if s is None:
            raise ValueError('string argument is required')

        parts = s.split(':')

        try:
            # Is the first part on OID?
            _oid = univ.ObjectIdentifier(parts[0])

            # It is; construct a V2 template
            # pylint: disable=too-many-function-args
            return MSCSTemplateV2.__new__(MSCSTemplateV2, s)

        except pyasn1.error.PyAsn1Error:
            # It is not an OID; treat as a template name
            # pylint: disable=too-many-function-args
            return MSCSTemplateV1.__new__(MSCSTemplateV1, s)

    def __getstate__(self):
        return self.unparsed_input

    def __setstate__(self, state):
        # explicitly call __init__ method to initialise object
        self.__init__(state)


class MSCSTemplate(ExternalCAProfile):
    """
    An Microsoft AD-CS Template specifier.

    Subclasses MUST set ext_oid.

    Subclass constructors MUST set asn1obj.

    """
    valid_for = set([ExternalCAType.MS_CS.value])

    ext_oid = None  # extension OID, as a Python str
    asn1obj = None  # unencoded extension data

    def get_ext_data(self):
        """Return DER-encoded extension data."""
        return encoder.encode(self.asn1obj)


class MSCSTemplateV1(MSCSTemplate):
    """
    A v1 template specifier, per
    https://msdn.microsoft.com/en-us/library/cc250011.aspx.

    ::

        CertificateTemplateName ::= SEQUENCE {
           Name            UTF8String
        }

    But note that a bare BMPString is used in practice.

    """
    ext_oid = "1.3.6.1.4.1.311.20.2"

    def __init__(self, s):
        super(MSCSTemplateV1, self).__init__(s)
        parts = s.split(':')
        if len(parts) > 1:
            raise ValueError(
                "Cannot specify certificate template version when using name.")
        self.asn1obj = char.BMPString(six.text_type(parts[0]))


class MSCSTemplateV2(MSCSTemplate):
    """
    A v2 template specifier, per
    https://msdn.microsoft.com/en-us/library/windows/desktop/aa378274(v=vs.85).aspx

    ::

        CertificateTemplate ::= SEQUENCE {
            templateID              EncodedObjectID,
            templateMajorVersion    TemplateVersion,
            templateMinorVersion    TemplateVersion OPTIONAL
        }

        TemplateVersion ::= INTEGER (0..4294967295)

    """
    ext_oid = "1.3.6.1.4.1.311.21.7"

    @staticmethod
    def check_version_in_range(desc, n):
        if n < 0 or n >= 2**32:
            raise ValueError(
                "Template {} version must be in range 0..4294967295"
                .format(desc))

    def __init__(self, s):
        super(MSCSTemplateV2, self).__init__(s)

        parts = s.split(':')

        obj = CertificateTemplateV2()
        if len(parts) < 2 or len(parts) > 3:
            raise ValueError(
                "Incorrect template specification; required format is: "
                "<oid>:<majorVersion>[:<minorVersion>]")
        try:
            obj['templateID'] = univ.ObjectIdentifier(parts[0])

            major = int(parts[1])
            self.check_version_in_range("major", major)
            obj['templateMajorVersion'] = major

            if len(parts) > 2:
                minor = int(parts[2])
                self.check_version_in_range("minor", minor)
                obj['templateMinorVersion'] = int(parts[2])

        except pyasn1.error.PyAsn1Error:
            raise ValueError("Could not parse certificate template specifier.")
        self.asn1obj = obj


class CertificateTemplateV2(univ.Sequence):
    componentType = namedtype.NamedTypes(
        namedtype.NamedType('templateID', univ.ObjectIdentifier()),
        namedtype.NamedType('templateMajorVersion', univ.Integer()),
        namedtype.OptionalNamedType('templateMinorVersion', univ.Integer())
    )


if __name__ == "__main__":
    standard_logging_setup("install.log")
    ds = dsinstance.DsInstance()

    ca = CAInstance("EXAMPLE.COM")
    ca.configure_instance("catest.example.com", "password", "password")
