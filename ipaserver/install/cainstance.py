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

from __future__ import print_function

import array
import base64
import binascii
import dbus
import ldap
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import syslog
import time
import tempfile
import xml.dom.minidom
import shlex
import pipes

from six.moves import urllib
from six.moves.configparser import ConfigParser, RawConfigParser

import ipalib.constants
from ipalib import api
from ipalib import pkcs10, x509
from ipalib import errors

from ipaplatform import services
from ipaplatform.constants import constants
from ipaplatform.paths import paths
from ipaplatform.tasks import tasks

from ipapython import dogtag
from ipapython import certmonger
from ipapython import ipautil
from ipapython import ipaldap
from ipapython.certdb import get_ca_nickname
from ipapython.dn import DN
from ipapython.ipa_log_manager import log_mgr,\
    standard_logging_setup, root_logger

from ipaserver.install import certs
from ipaserver.install import bindinstance
from ipaserver.install import dsinstance
from ipaserver.install import installutils
from ipaserver.install import ldapupdate
from ipaserver.install import replication
from ipaserver.install import service
from ipaserver.install.dogtaginstance import (export_kra_agent_pem,
                                              DogtagInstance)
from ipaserver.plugins import ldap2

# Python 3 rename. The package is available in "six.moves.http_client", but
# pylint cannot handle classes from that alias
try:
    import httplib
except ImportError:
    import http.client as httplib


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


def check_port():
    """
    Check that dogtag port (8443) is available.

    Returns True when the port is free, False if it's taken.
    """
    return not ipautil.host_port_open(None, 8443)

def get_preop_pin(instance_root, instance_name):
    # Only used for Dogtag 9
    preop_pin = None

    filename = instance_root + "/" + instance_name + "/conf/CS.cfg"

    # read the config file and get the preop pin
    try:
        f = open(filename)
    except IOError as e:
        root_logger.error("Cannot open configuration file." + str(e))
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
    test = installutils.get_directive(path, 'preop.ca.type', '=')
    if test == "otherca":
        return True
    return False


def is_ca_installed_locally():
    """Check if CA is installed locally by checking for existence of CS.cfg
    :return:True/False
    """
    return os.path.exists(paths.CA_CS_CFG_PATH)


def create_ca_user():
    """Create PKI user/group if it doesn't exist yet."""
    tasks.create_system_user(
        name=constants.PKI_USER,
        group=constants.PKI_GROUP,
        homedir=paths.VAR_LIB,
        shell=paths.NOLOGIN,
    )


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

    tracking_reqs = (('auditSigningCert cert-pki-ca', None),
                     ('ocspSigningCert cert-pki-ca', None),
                     ('subsystemCert cert-pki-ca', None),
                     ('caSigningCert cert-pki-ca', 'ipaCACertRenewal'))
    server_cert_name = 'Server-Cert cert-pki-ca'

    def __init__(self, realm=None, ra_db=None, host_name=None,
                 dm_password=None, ldapi=True, api=api):
        super(CAInstance, self).__init__(
            realm=realm,
            subsystem="CA",
            service_desc="certificate server",
            host_name=host_name,
            dm_password=dm_password,
            ldapi=ldapi
        )

        # for external CAs
        self.external = 0
        self.csr_file = None
        self.cert_file = None
        self.cert_chain_file = None
        self.create_ra_agent_db = True
        self.api = api

        if realm is not None:
            self.canickname = get_ca_nickname(realm)
        else:
            self.canickname = None
        self.ra_agent_db = ra_db
        if self.ra_agent_db is not None:
            self.ra_agent_pwd = self.ra_agent_db + "/pwdfile.txt"
        else:
            self.ra_agent_pwd = None
        self.ra_cert = None
        self.requestId = None
        self.log = log_mgr.get_logger(self)
        self.no_db_setup = False

    def configure_instance(self, host_name, dm_password, admin_password,
                           pkcs12_info=None, master_host=None, csr_file=None,
                           cert_file=None, cert_chain_file=None,
                           master_replication_port=None,
                           subject_base=None, ca_signing_algorithm=None,
                           ca_type=None, ra_p12=None):
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
        self.admin_password = admin_password
        self.pkcs12_info = pkcs12_info
        if self.pkcs12_info is not None:
            self.clone = True
        self.master_host = master_host
        self.master_replication_port = master_replication_port
        if subject_base is None:
            self.subject_base = DN(('O', self.realm))
        else:
            self.subject_base = subject_base
        if ca_signing_algorithm is None:
            self.ca_signing_algorithm = 'SHA256withRSA'
        else:
            self.ca_signing_algorithm = ca_signing_algorithm
        if ca_type is not None:
            self.ca_type = ca_type
        else:
            self.ca_type = 'generic'

        # Determine if we are installing as an externally-signed CA and
        # what stage we're in.
        if csr_file is not None:
            self.csr_file = csr_file
            self.external = 1
        elif cert_file is not None:
            self.cert_file = cert_file
            self.cert_chain_file = cert_chain_file
            self.external = 2

        self.step("creating certificate server user", create_ca_user)
        self.step("configuring certificate server instance",
                  self.__spawn_instance)
        self.step("stopping certificate server instance to update CS.cfg", self.stop_instance)
        self.step("backing up CS.cfg", self.backup_config)
        self.step("disabling nonces", self.__disable_nonce)
        self.step("set up CRL publishing", self.__enable_crl_publish)
        self.step("enable PKIX certificate path discovery and validation", self.enable_pkix)
        self.step("starting certificate server instance", self.start_instance)
        # Step 1 of external is getting a CSR so we don't need to do these
        # steps until we get a cert back from the external CA.
        if self.external != 1:
            if self.create_ra_agent_db:
                self.step("creating RA agent certificate database", self.__create_ra_agent_db)
            self.step("importing CA chain to RA certificate database", self.__import_ca_chain)
            self.step("fixing RA database permissions", self.fix_ra_perms)
            self.step("setting up signing cert profile", self.__setup_sign_profile)
            self.step("setting audit signing renewal to 2 years", self.set_audit_renewal)
            if not self.clone:
                self.step("restarting certificate server", self.restart_instance)
                self.step("requesting RA certificate from CA", self.__request_ra_certificate)
                self.step("issuing RA agent certificate", self.__issue_ra_cert)
                self.step("adding RA agent as a trusted user", self.__create_ca_agent)
            elif ra_p12 is not None:
                self.step("importing RA certificate from PKCS #12 file",
                          lambda: self.import_ra_cert(ra_p12, configure_renewal=False))
            self.step("authorizing RA to modify profiles", configure_profiles_acl)
            self.step("configure certmonger for renewals", self.configure_certmonger_renewal)
            self.step("configure certificate renewals", self.configure_renewal)
            if not self.clone:
                self.step("configure RA certificate renewal", self.configure_agent_renewal)
            self.step("configure Server-Cert certificate renewal", self.track_servercert)
            self.step("Configure HTTP to proxy connections",
                      self.http_proxy)
            self.step("restarting certificate server", self.restart_instance)
            self.step("migrating certificate profiles to LDAP",
                      migrate_profiles_to_ldap)
            self.step("importing IPA certificate profiles",
                      import_included_profiles)
            self.step("adding default CA ACL", ensure_default_caacl)
            self.step("updating IPA configuration", update_ipa_conf)

        self.start_creation(runtime=210)

    def __spawn_instance(self):
        """
        Create and configure a new CA instance using pkispawn.
        Creates the config file with IPA specific parameters
        and passes it to the base class to call pkispawn
        """

        # Create an empty and secured file
        (cfg_fd, cfg_file) = tempfile.mkstemp()
        os.close(cfg_fd)
        pent = pwd.getpwnam(constants.PKI_USER)
        os.chown(cfg_file, pent.pw_uid, pent.pw_gid)

        # Create CA configuration
        config = ConfigParser()
        config.optionxform = str
        config.add_section("CA")

        # Server
        config.set("CA", "pki_security_domain_name", self.security_domain_name)
        config.set("CA", "pki_enable_proxy", "True")
        config.set("CA", "pki_restart_configured_instance", "False")
        config.set("CA", "pki_backup_keys", "True")
        config.set("CA", "pki_backup_password", self.admin_password)
        config.set("CA", "pki_profiles_in_ldap", "True")

        # Client security database
        config.set("CA", "pki_client_database_dir", self.agent_db)
        config.set("CA", "pki_client_database_password", self.admin_password)
        config.set("CA", "pki_client_database_purge", "False")
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
        config.set("CA", "pki_ds_base_dn", self.basedn)
        config.set("CA", "pki_ds_database", "ipaca")

        if not self.create_ra_agent_db and not self.clone:
            self._use_ldaps_during_spawn(config)

        # Certificate subject DN's
        config.set("CA", "pki_subsystem_subject_dn",
            str(DN(('cn', 'CA Subsystem'), self.subject_base)))
        config.set("CA", "pki_ocsp_signing_subject_dn",
            str(DN(('cn', 'OCSP Subsystem'), self.subject_base)))
        config.set("CA", "pki_ssl_server_subject_dn",
            str(DN(('cn', self.fqdn), self.subject_base)))
        config.set("CA", "pki_audit_signing_subject_dn",
            str(DN(('cn', 'CA Audit'), self.subject_base)))
        config.set("CA", "pki_ca_signing_subject_dn",
            str(DN(('cn', 'Certificate Authority'), self.subject_base)))

        # Certificate nicknames
        config.set("CA", "pki_subsystem_nickname", "subsystemCert cert-pki-ca")
        config.set("CA", "pki_ocsp_signing_nickname", "ocspSigningCert cert-pki-ca")
        config.set("CA", "pki_ssl_server_nickname", "Server-Cert cert-pki-ca")
        config.set("CA", "pki_audit_signing_nickname", "auditSigningCert cert-pki-ca")
        config.set("CA", "pki_ca_signing_nickname", "caSigningCert cert-pki-ca")

        # CA key algorithm
        config.set("CA", "pki_ca_signing_key_algorithm", self.ca_signing_algorithm)

        if self.clone:

            if self.no_db_setup:
                config.set("CA", "pki_ds_create_new_db", "False")
                config.set("CA", "pki_clone_setup_replication", "False")
                config.set("CA", "pki_clone_reindex_data", "True")

            cafile = self.pkcs12_info[0]
            shutil.copy(cafile, paths.TMP_CA_P12)
            pent = pwd.getpwnam(constants.PKI_USER)
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

            if self.ca_type == 'ms-cs':
                # Include MS template name extension in the CSR
                config.set("CA", "pki_req_ext_add", "True")
                config.set("CA", "pki_req_ext_oid", "1.3.6.1.4.1.311.20.2")
                config.set("CA", "pki_req_ext_critical", "False")
                config.set("CA", "pki_req_ext_data", "1E0A00530075006200430041")

        elif self.external == 2:
            cert = x509.load_certificate_from_file(self.cert_file)
            cert_file = tempfile.NamedTemporaryFile()
            x509.write_certificate(cert.der_data, cert_file.name)
            cert_file.flush()

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
        with open(cfg_file, "wb") as f:
            config.write(f)

        self.backup_state('installed', True)
        try:
            DogtagInstance.spawn_instance(self, cfg_file)
        finally:
            os.remove(cfg_file)

        if self.external == 1:
            print("The next step is to get %s signed by your CA and re-run %s as:" % (self.csr_file, sys.argv[0]))
            print("%s --external-cert-file=/path/to/signed_certificate --external-cert-file=/path/to/external_ca_certificate" % sys.argv[0])
            sys.exit(0)
        else:
            shutil.move(paths.CA_BACKUP_KEYS_P12,
                        paths.CACERT_P12)

        self.log.debug("completed creating ca instance")

    def backup_config(self):
        try:
            backup_config()
        except Exception as e:
            root_logger.warning("Failed to backup CS.cfg: %s", e)

    def __update_topology(self):
        ld = ldapupdate.LDAPUpdate(ldapi=True, sub_dict={
            'SUFFIX': api.env.basedn,
            'FQDN': self.fqdn,
        })
        ld.update([paths.CA_TOPOLOGY_ULDIF])

    def __disable_nonce(self):
        # Turn off Nonces
        update_result = installutils.update_file(
            paths.CA_CS_CFG_PATH, 'ca.enableNonces=true',
            'ca.enableNonces=false')
        if update_result != 0:
            raise RuntimeError("Disabling nonces failed")
        pent = pwd.getpwnam(constants.PKI_USER)
        os.chown(paths.CA_CS_CFG_PATH, pent.pw_uid, pent.pw_gid)

    def enable_pkix(self):
        installutils.set_directive(paths.SYSCONFIG_PKI_TOMCAT,
                                   'NSS_ENABLE_PKIX_VERIFY', '1',
                                   quotes=False, separator='=')

    def __issue_ra_cert(self):
        # The CA certificate is in the agent DB but isn't trusted
        (admin_fd, admin_name) = tempfile.mkstemp()
        os.write(admin_fd, self.admin_password)
        os.close(admin_fd)

        # Look through the cert chain to get all the certs we need to add
        # trust for
        args = [paths.CERTUTIL,
                "-d", self.agent_db,
                "-O",
                "-n", "ipa-ca-agent"]
        result = ipautil.run(args, capture_output=True)
        chain = result.output.split("\n")

        root_nickname=[]
        for part in chain:
            m = re.match('\ *"(.*)" \[.*', part)
            if m:
                nick = m.groups(0)[0]
                if nick != "ipa-ca-agent" and nick[:7] != "Builtin":
                    root_nickname.append(m.groups()[0])

        try:
            for nick in root_nickname:
                self.__run_certutil(
                    ['-M', '-t', 'CT,C,C', '-n',
                     nick],
                     database=self.agent_db, pwd_file=self.admin_password)
        finally:
            os.remove(admin_name)

        # Retrieve the certificate request so we can get the values needed
        # to issue a certificate. Use sslget here because this is a
        # temporary database and nsslib doesn't currently support gracefully
        # opening and closing an NSS database. This would leave the installer
        # process stuck using this database during the entire cycle. We need
        # to use the final RA agent database when issuing certs for DS and
        # mod_nss.
        args = [
            paths.SSLGET,
            '-v',
            '-n', 'ipa-ca-agent',
            '-p', self.admin_password,
            '-d', self.agent_db,
            '-r', '/ca/agent/ca/profileReview?requestId=%s' % self.requestId,
            '%s' % ipautil.format_netloc(self.fqdn, 8443),
        ]
        result = ipautil.run(
            args, nolog=(self.admin_password,),
            capture_output=True)

        data = result.output.split('\n')
        params = get_defList(data)
        params['requestId'] = find_substring(data, "requestId")
        params['op'] = 'approve'
        params['submit'] = 'submit'
        params['requestNotes'] = ''
        params = urllib.parse.urlencode(params)

        # Now issue the RA certificate.
        args = [
            paths.SSLGET,
            '-v',
            '-n', 'ipa-ca-agent',
            '-p', self.admin_password,
            '-d', self.agent_db,
            '-e', params,
            '-r', '/ca/agent/ca/profileProcess',
            '%s' % ipautil.format_netloc(self.fqdn, 8443),
        ]
        result = ipautil.run(
            args, nolog=(self.admin_password,),
            capture_output=True)

        data = result.output.split('\n')
        outputList = get_outputList(data)

        self.ra_cert = outputList['b64_cert']

        # Strip certificate headers and convert it to proper line ending
        self.ra_cert = x509.strip_header(self.ra_cert)
        self.ra_cert = "\n".join(line.strip() for line
                                 in self.ra_cert.splitlines() if line.strip())

        # Add the new RA cert to the database in /etc/httpd/alias
        (agent_fd, agent_name) = tempfile.mkstemp()
        os.write(agent_fd, self.ra_cert)
        os.close(agent_fd)
        try:
            self.__run_certutil(
                ['-A', '-t', 'u,u,u', '-n', 'ipaCert', '-a',
                 '-i', agent_name]
            )
        finally:
            os.remove(agent_name)

        export_kra_agent_pem()

    def import_ra_cert(self, rafile, configure_renewal=True):
        """
        Cloned RAs will use the same RA agent cert as the master so we
        need to import from a PKCS#12 file.

        Used when setting up replication
        """
        # Add the new RA cert to the database in /etc/httpd/alias
        (agent_fd, agent_name) = tempfile.mkstemp()
        os.write(agent_fd, self.dm_password)
        os.close(agent_fd)
        try:
            import_pkcs12(rafile, agent_name, self.ra_agent_db, self.ra_agent_pwd)
        finally:
            os.remove(agent_name)

        if configure_renewal:
            self.configure_agent_renewal()

        export_kra_agent_pem()

    def __create_ca_agent(self):
        """
        Create CA agent, assign a certificate, and add the user to
        the appropriate groups for accessing CA services.
        """

        # get ipaCert certificate
        cert_data = base64.b64decode(self.ra_cert)
        cert = x509.load_certificate(cert_data, x509.DER)

        # connect to CA database
        server_id = installutils.realm_to_serverid(api.env.realm)
        dogtag_uri = 'ldapi://%%2fvar%%2frun%%2fslapd-%s.socket' % server_id
        conn = ldap2.ldap2(api, ldap_uri=dogtag_uri)
        conn.connect(autobind=True)

        # create ipara user with ipaCert certificate
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
            userCertificate=[cert_data],
            description=['2;%s;%s;%s' % (
                cert.serial_number,
                DN(('CN', 'Certificate Authority'), self.subject_base),
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

    def __run_certutil(self, args, database=None, pwd_file=None, stdin=None,
                       **kwargs):
        if not database:
            database = self.ra_agent_db
        if not pwd_file:
            pwd_file = self.ra_agent_pwd
        new_args = [paths.CERTUTIL, "-d", database, "-f", pwd_file]
        new_args = new_args + args
        return ipautil.run(new_args, stdin, nolog=(pwd_file,), **kwargs)

    def __create_ra_agent_db(self):
        if ipautil.file_exists(self.ra_agent_db + "/cert8.db"):
            ipautil.backup_file(self.ra_agent_db + "/cert8.db")
            ipautil.backup_file(self.ra_agent_db + "/key3.db")
            ipautil.backup_file(self.ra_agent_db + "/secmod.db")
            ipautil.backup_file(self.ra_agent_db + "/pwdfile.txt")

        if not ipautil.dir_exists(self.ra_agent_db):
            os.mkdir(self.ra_agent_db)
            os.chmod(self.ra_agent_db, 0o755)

        # Create the password file for this db
        hex_str = binascii.hexlify(os.urandom(10))
        f = os.open(self.ra_agent_pwd, os.O_CREAT | os.O_RDWR)
        os.write(f, hex_str)
        os.close(f)
        os.chmod(self.ra_agent_pwd, stat.S_IRUSR)

        self.__run_certutil(["-N"])

    def __get_ca_chain(self):
        try:
            return dogtag.get_ca_certchain(ca_host=self.fqdn)
        except Exception as e:
            raise RuntimeError("Unable to retrieve CA chain: %s" % str(e))

    def __import_ca_chain(self):
        chain = self.__get_ca_chain()

        # If this chain contains multiple certs then certutil will only import
        # the first one. So we have to pull them all out and import them
        # separately. Unfortunately no NSS tool can do this so we have to
        # use openssl.

        # Convert to DER because the chain comes back as one long string which
        # makes openssl throw up.
        data = base64.b64decode(chain)

        result = ipautil.run(
            [paths.OPENSSL,
             "pkcs7",
             "-inform",
             "DER",
             "-print_certs",
             ], stdin=data, capture_output=True)
        certlist = result.output

        # Ok, now we have all the certificates in certs, walk through it
        # and pull out each certificate and add it to our database

        st = 1
        en = 0
        subid = 0
        ca_dn = DN(('CN','Certificate Authority'), self.subject_base)
        while st > 0:
            st = certlist.find('-----BEGIN', en)
            en = certlist.find('-----END', en+1)
            if st > 0:
                try:
                    (chain_fd, chain_name) = tempfile.mkstemp()
                    os.write(chain_fd, certlist[st:en+25])
                    os.close(chain_fd)
                    (_rdn, subject_dn) = certs.get_cert_nickname(certlist[st:en+25])
                    if subject_dn == ca_dn:
                        nick = get_ca_nickname(self.realm)
                        trust_flags = 'CT,C,C'
                    else:
                        nick = str(subject_dn)
                        trust_flags = ',,'
                    self.__run_certutil(
                        ['-A', '-t', trust_flags, '-n', nick, '-a',
                         '-i', chain_name]
                    )
                finally:
                    os.remove(chain_name)
                    subid += 1

    def __request_ra_certificate(self):
        # Create a noise file for generating our private key
        noise = array.array('B', os.urandom(128))
        (noise_fd, noise_name) = tempfile.mkstemp()
        os.write(noise_fd, noise)
        os.close(noise_fd)

        # Generate our CSR. The result gets put into stdout
        try:
            result = self.__run_certutil(
                ["-R", "-k", "rsa", "-g", "2048", "-s",
                 str(DN(('CN', 'IPA RA'), self.subject_base)),
                 "-z", noise_name, "-a"],
                capture_output=True)
        finally:
            os.remove(noise_name)

        csr = pkcs10.strip_header(result.output)

        # Send the request to the CA
        conn = httplib.HTTPConnection(self.fqdn, 8080)
        params = urllib.parse.urlencode({'profileId': 'caServerCert',
                'cert_request_type': 'pkcs10',
                'requestor_name': 'IPA Installer',
                'cert_request': csr,
                'xmlOutput': 'true'})
        headers = {"Content-type": "application/x-www-form-urlencoded",
                   "Accept": "text/plain"}

        conn.request("POST", "/ca/ee/ca/profileSubmit", params, headers)
        res = conn.getresponse()
        if res.status == 200:
            data = res.read()
            conn.close()
            doc = xml.dom.minidom.parseString(data)
            item_node = doc.getElementsByTagName("RequestId")
            self.requestId = item_node[0].childNodes[0].data
            doc.unlink()
            self.requestId = self.requestId.strip()
            if self.requestId is None:
                raise RuntimeError("Unable to determine RA certificate requestId")
        else:
            conn.close()
            raise RuntimeError("Unable to submit RA cert request")

    def fix_ra_perms(self):
        os.chmod(self.ra_agent_db + "/cert8.db", 0o640)
        os.chmod(self.ra_agent_db + "/key3.db", 0o640)
        os.chmod(self.ra_agent_db + "/secmod.db", 0o640)

        pent = pwd.getpwnam(constants.HTTPD_USER)
        os.chown(self.ra_agent_db + "/cert8.db", 0, pent.pw_gid )
        os.chown(self.ra_agent_db + "/key3.db", 0, pent.pw_gid )
        os.chown(self.ra_agent_db + "/secmod.db", 0, pent.pw_gid )
        os.chown(self.ra_agent_pwd, pent.pw_uid, pent.pw_gid)

    def __setup_sign_profile(self):
        # Tell the profile to automatically issue certs for RAs
        installutils.set_directive(
            paths.CAJARSIGNINGCERT_CFG, 'auth.instance_id', 'raCertAuth',
            quotes=False, separator='=')

    def prepare_crl_publish_dir(self):
        """
        Prepare target directory for CRL publishing

        Returns a path to the CRL publishing directory
        """
        publishdir = paths.PKI_CA_PUBLISH_DIR

        if not os.path.exists(publishdir):
            os.mkdir(publishdir)

        os.chmod(publishdir, 0o775)
        pent = pwd.getpwnam(constants.PKI_USER)
        os.chown(publishdir, 0, pent.pw_gid)

        tasks.restore_context(publishdir)

        return publishdir


    def __enable_crl_publish(self):
        """
        Enable file-based CRL publishing and disable LDAP publishing.

        https://access.redhat.com/knowledge/docs/en-US/Red_Hat_Certificate_System/8.0/html/Admin_Guide/Setting_up_Publishing.html
        """
        caconfig = paths.CA_CS_CFG_PATH

        publishdir = self.prepare_crl_publish_dir()

        # Enable file publishing, disable LDAP
        installutils.set_directive(caconfig, 'ca.publish.enable', 'true', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.ldappublish.enable', 'false', quotes=False, separator='=')

        # Create the file publisher, der only, not b64
        installutils.set_directive(caconfig, 'ca.publish.publisher.impl.FileBasedPublisher.class','com.netscape.cms.publish.publishers.FileBasedPublisher', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.publisher.instance.FileBaseCRLPublisher.crlLinkExt', 'bin', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.publisher.instance.FileBaseCRLPublisher.directory', publishdir, quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.publisher.instance.FileBaseCRLPublisher.latestCrlLink', 'true', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.publisher.instance.FileBaseCRLPublisher.pluginName', 'FileBasedPublisher', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.publisher.instance.FileBaseCRLPublisher.timeStamp', 'LocalTime', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.publisher.instance.FileBaseCRLPublisher.zipCRLs', 'false', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.publisher.instance.FileBaseCRLPublisher.zipLevel', '9', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.publisher.instance.FileBaseCRLPublisher.Filename.b64', 'false', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.publisher.instance.FileBaseCRLPublisher.Filename.der', 'true', quotes=False, separator='=')

        # The publishing rule
        installutils.set_directive(caconfig, 'ca.publish.rule.instance.FileCrlRule.enable', 'true', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.rule.instance.FileCrlRule.mapper', 'NoMap', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.rule.instance.FileCrlRule.pluginName', 'Rule', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.rule.instance.FileCrlRule.predicate=', '', quotes=False, separator='')
        installutils.set_directive(caconfig, 'ca.publish.rule.instance.FileCrlRule.publisher', 'FileBaseCRLPublisher', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.rule.instance.FileCrlRule.type', 'crl', quotes=False, separator='=')

        # Now disable LDAP publishing
        installutils.set_directive(caconfig, 'ca.publish.rule.instance.LdapCaCertRule.enable', 'false', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.rule.instance.LdapCrlRule.enable', 'false', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.rule.instance.LdapUserCertRule.enable', 'false', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.rule.instance.LdapXCertRule.enable', 'false', quotes=False, separator='=')

        # If we are the initial master then we are the CRL generator, otherwise
        # we point to that master for CRLs.
        if not self.clone:
            # These next two are defaults, but I want to be explicit that the
            # initial master is the CRL generator.
            installutils.set_directive(caconfig, 'ca.crl.MasterCRL.enableCRLCache', 'true', quotes=False, separator='=')
            installutils.set_directive(caconfig, 'ca.crl.MasterCRL.enableCRLUpdates', 'true', quotes=False, separator='=')
            installutils.set_directive(caconfig, 'ca.listenToCloneModifications', 'true', quotes=False, separator='=')
        else:
            installutils.set_directive(caconfig, 'ca.crl.MasterCRL.enableCRLCache', 'false', quotes=False, separator='=')
            installutils.set_directive(caconfig, 'ca.crl.MasterCRL.enableCRLUpdates', 'false', quotes=False, separator='=')
            installutils.set_directive(caconfig, 'ca.listenToCloneModifications', 'false', quotes=False, separator='=')

    def uninstall(self):
        # just eat state
        self.restore_state("enabled")

        DogtagInstance.uninstall(self)

        self.restore_state("installed")

        # At one time we removed this user on uninstall. That can potentially
        # orphan files, or worse, if another useradd runs in the interim,
        # cause files to have a new owner.
        self.restore_state("user_exists")

        services.knownservices.messagebus.start()
        cmonger = services.knownservices.certmonger
        cmonger.start()

        bus = dbus.SystemBus()
        obj = bus.get_object('org.fedorahosted.certmonger',
                             '/org/fedorahosted/certmonger')
        iface = dbus.Interface(obj, 'org.fedorahosted.certmonger')
        path = iface.find_ca_by_nickname('dogtag-ipa-ca-renew-agent')
        if path:
            iface.remove_known_ca(path)

        helper = self.restore_state('certmonger_dogtag_helper')
        if helper:
            path = iface.find_ca_by_nickname('dogtag-ipa-renew-agent')
            if path:
                ca_obj = bus.get_object('org.fedorahosted.certmonger', path)
                ca_iface = dbus.Interface(ca_obj,
                                          'org.freedesktop.DBus.Properties')
                ca_iface.Set('org.fedorahosted.certmonger.ca',
                             'external-helper', helper)

        cmonger.stop()

        # remove CRL files
        self.log.info("Remove old CRL files")
        try:
            for f in get_crl_files():
                self.log.debug("Remove %s", f)
                installutils.remove_file(f)
        except OSError as e:
            self.log.warning("Error while removing old CRL files: %s", e)

        # remove CRL directory
        self.log.info("Remove CRL directory")
        if os.path.exists(paths.PKI_CA_PUBLISH_DIR):
            try:
                shutil.rmtree(paths.PKI_CA_PUBLISH_DIR)
            except OSError as e:
                self.log.warning("Error while removing CRL publish "
                                    "directory: %s", e)

    def publish_ca_cert(self, location):
        args = ["-L", "-n", self.canickname, "-a"]
        result = self.__run_certutil(
            args, capture_output=True)
        cert = result.output
        fd = open(location, "w+")
        fd.write(cert)
        fd.close()
        os.chmod(location, 0o444)


    def configure_certmonger_renewal(self):
        super(CAInstance, self).configure_certmonger_renewal()

        self.configure_certmonger_renewal_guard()

    def configure_certmonger_renewal_guard(self):
        if not self.is_configured():
            return

        bus = dbus.SystemBus()
        obj = bus.get_object('org.fedorahosted.certmonger',
                             '/org/fedorahosted/certmonger')
        iface = dbus.Interface(obj, 'org.fedorahosted.certmonger')
        path = iface.find_ca_by_nickname('dogtag-ipa-renew-agent')
        if path:
            ca_obj = bus.get_object('org.fedorahosted.certmonger', path)
            ca_iface = dbus.Interface(ca_obj,
                                      'org.freedesktop.DBus.Properties')
            helper = ca_iface.Get('org.fedorahosted.certmonger.ca',
                                  'external-helper')
            if helper:
                args = shlex.split(helper)
                if args[0] != paths.IPA_SERVER_GUARD:
                    self.backup_state('certmonger_dogtag_helper', helper)
                    args = [paths.IPA_SERVER_GUARD] + args
                    helper = ' '.join(pipes.quote(a) for a in args)
                    ca_iface.Set('org.fedorahosted.certmonger.ca',
                                 'external-helper', helper)

    def configure_agent_renewal(self):
        try:
            certmonger.dogtag_start_tracking(
                ca='dogtag-ipa-ca-renew-agent',
                nickname='ipaCert',
                pin=None,
                pinfile=paths.ALIAS_PWDFILE_TXT,
                secdir=paths.HTTPD_ALIAS_DIR,
                pre_command='renew_ra_cert_pre',
                post_command='renew_ra_cert')
        except RuntimeError as e:
            self.log.error(
                "certmonger failed to start tracking certificate: %s", e)

    def stop_tracking_certificates(self):
        """Stop tracking our certificates. Called on uninstall.
        """
        super(CAInstance, self).stop_tracking_certificates(False)

        try:
            certmonger.stop_tracking(paths.HTTPD_ALIAS_DIR, nickname='ipaCert')
        except RuntimeError as e:
            root_logger.error(
                "certmonger failed to stop tracking certificate: %s", e)

        services.knownservices.certmonger.stop()


    def set_audit_renewal(self):
        """
        The default renewal time for the audit signing certificate is
        six months rather than two years. Fix it. This is BZ 843979.
        """
        # Check the default validity period of the audit signing cert
        # and set it to 2 years if it is 6 months.
        cert_range = installutils.get_directive(
            paths.CASIGNEDLOGCERT_CFG,
            'policyset.caLogSigningSet.2.default.params.range',
            separator='='
        )
        self.log.debug(
            'caSignedLogCert.cfg profile validity range is %s', cert_range)
        if cert_range == "180":
            installutils.set_directive(
                paths.CASIGNEDLOGCERT_CFG,
                'policyset.caLogSigningSet.2.default.params.range',
                '720',
                quotes=False,
                separator='='
            )
            installutils.set_directive(
                paths.CASIGNEDLOGCERT_CFG,
                'policyset.caLogSigningSet.2.constraint.params.range',
                '720',
                quotes=False,
                separator='='
            )
            self.log.debug(
                'updated caSignedLogCert.cfg profile validity range to 720')
            return True
        return False

    def is_renewal_master(self, fqdn=None):
        if fqdn is None:
            fqdn = api.env.host

        if not self.admin_conn:
            self.ldap_connect()

        dn = DN(('cn', 'CA'), ('cn', fqdn), ('cn', 'masters'), ('cn', 'ipa'),
                ('cn', 'etc'), api.env.basedn)
        renewal_filter = '(ipaConfigString=caRenewalMaster)'
        try:
            self.admin_conn.get_entries(base_dn=dn, filter=renewal_filter,
                                        attrs_list=[])
        except errors.NotFound:
            return False

        return True

    def set_renewal_master(self, fqdn=None):
        if fqdn is None:
            fqdn = api.env.host

        if not self.admin_conn:
            self.ldap_connect()

        base_dn = DN(('cn', 'masters'), ('cn', 'ipa'), ('cn', 'etc'),
                     api.env.basedn)
        filter = '(&(cn=CA)(ipaConfigString=caRenewalMaster))'
        try:
            entries = self.admin_conn.get_entries(
                base_dn=base_dn, filter=filter, attrs_list=['ipaConfigString'])
        except errors.NotFound:
            entries = []

        dn = DN(('cn', 'CA'), ('cn', fqdn), base_dn)
        master_entry = self.admin_conn.get_entry(dn, ['ipaConfigString'])

        for entry in entries:
            if master_entry is not None and entry.dn == master_entry.dn:
                master_entry = None
                continue

            entry['ipaConfigString'] = [x for x in entry['ipaConfigString']
                                        if x.lower() != 'carenewalmaster']
            self.admin_conn.update_entry(entry)

        if master_entry is not None:
            master_entry['ipaConfigString'].append('caRenewalMaster')
            self.admin_conn.update_entry(master_entry)

    @staticmethod
    def update_cert_config(nickname, cert):
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
            backup_config()
        except Exception as e:
            syslog.syslog(syslog.LOG_ERR, "Failed to backup CS.cfg: %s" % e)

        DogtagInstance.update_cert_cs_cfg(
            nickname, cert, directives, paths.CA_CS_CFG_PATH)

    def __create_ds_db(self):
        '''
        Create PKI database. Is needed when pkispawn option
        pki_ds_create_new_db is set to False
        '''

        if not self.admin_conn:
            self.ldap_connect()

        backend = 'ipaca'
        suffix = DN(('o', 'ipaca'))

        # replication
        dn = DN(('cn', str(suffix)), ('cn', 'mapping tree'), ('cn', 'config'))
        entry = self.admin_conn.make_entry(
            dn,
            objectclass=["top", "extensibleObject", "nsMappingTree"],
            cn=[suffix],
        )
        entry['nsslapd-state'] = ['Backend']
        entry['nsslapd-backend'] = [backend]
        self.admin_conn.add_entry(entry)

        # database
        dn = DN(('cn', 'ipaca'), ('cn', 'ldbm database'), ('cn', 'plugins'),
                ('cn', 'config'))
        entry = self.admin_conn.make_entry(
            dn,
            objectclass=["top", "extensibleObject", "nsBackendInstance"],
            cn=[backend],
        )
        entry['nsslapd-suffix'] = [suffix]
        self.admin_conn.add_entry(entry)

    def __setup_replication(self):

        repl = replication.CAReplicationManager(self.realm, self.fqdn)
        repl.setup_cs_replication(self.master_host)

        # Activate Topology for o=ipaca segments
        self.__update_topology()

    def __client_auth_to_db(self):
        self.enable_client_auth_to_db(paths.CA_CS_CFG_PATH)

    def __restart_http_instance(self):
        # We need to restart apache as we drop a new config file in there
        services.knownservices.httpd.restart(capture_output=True)

    def __enable_instance(self):
        basedn = ipautil.realm_to_suffix(self.realm)
        self.ldap_enable('CA', self.fqdn, None, basedn)

    def __update_ca_records(self):
        # Install CA DNS records
        if bindinstance.dns_container_exists(
            api.env.host, api.env.basedn, ldapi=True, realm=api.env.realm
        ):
            bind = bindinstance.BindInstance(ldapi=True, api=self.api)
            bind.add_ipa_ca_dns_records(api.env.host, api.env.domain)

    def configure_replica(self, master_host, subject_base=None,
                          ca_cert_bundle=None, ca_signing_algorithm=None,
                          ca_type=None):
        """Creates a replica CA, creating a local DS backend and using
        the topology plugin to manage replication.
        Requires domain_level >= DOMAIN_LEVEL_1 and custodia on the master.
        """
        self.master_host = master_host
        self.master_replication_port = 389
        if subject_base is None:
            self.subject_base = DN(('O', self.realm))
        else:
            self.subject_base = subject_base
        if ca_signing_algorithm is None:
            self.ca_signing_algorithm = 'SHA256withRSA'
        else:
            self.ca_signing_algorithm = ca_signing_algorithm
        if ca_type is not None:
            self.ca_type = ca_type
        else:
            self.ca_type = 'generic'

        self.admin_groups = ADMIN_GROUPS
        self.pkcs12_info = ca_cert_bundle
        self.no_db_setup = True
        self.clone = True

        # TODO: deal with "Externally signed CA setups"

        # Set up steps
        self.step("creating certificate server user", create_ca_user)

        # Setup Database
        self.step("creating certificate server db", self.__create_ds_db)
        self.step("setting up initial replication", self.__setup_replication)

        self.step("creating installation admin user", self.setup_admin)

        # Setup instance
        self.step("setting up certificate server", self.__spawn_instance)
        self.step("stopping instance to update CS.cfg", self.stop_instance)
        self.step("backing up CS.cfg", self.backup_config)
        self.step("disabling nonces", self.__disable_nonce)
        self.step("set up CRL publishing", self.__enable_crl_publish)
        self.step("enable PKIX certificate path discovery and validation",
                  self.enable_pkix)
        self.step("set up client auth to db", self.__client_auth_to_db)
        self.step("destroying installation admin user", self.teardown_admin)
        self.step("starting instance", self.start_instance)

        self.step("importing CA chain to RA certificate database",
                  self.__import_ca_chain)
        self.step("fixing RA database permissions", self.fix_ra_perms)
        self.step("setting up signing cert profile", self.__setup_sign_profile)
        self.step("setting audit signing renewal to 2 years",
                  self.set_audit_renewal)

        self.step("configure certificate renewals",
                  self.configure_renewal)
        self.step("configure Server-Cert certificate renewal",
                  self.track_servercert)
        self.step("Configure HTTP to proxy connections",
                  self.http_proxy)
        self.step("updating IPA configuration", update_ipa_conf)
        self.step("Restart HTTP server to pick up changes",
                  self.__restart_http_instance)

        self.step("enabling CA instance", self.__enable_instance)
        self.step("Updating DNS CA records", self.__update_ca_records)

        self.start_creation(runtime=210)


def replica_ca_install_check(config):
    if not config.setup_ca:
        return

    cafile = config.dir + "/cacert.p12"
    if not ipautil.file_exists(cafile):
        # Replica of old "self-signed" master - CA won't be installed
        return

    if config.ca_ds_port != 7389:
        root_logger.debug(
            'Installing CA Replica from master with a merged database')
        return

    # Check if the master has the necessary schema in its CA instance
    ca_ldap_url = 'ldap://%s:%s' % (config.master_host_name, config.ca_ds_port)
    objectclass = 'ipaObject'
    root_logger.debug('Checking if IPA schema is present in %s', ca_ldap_url)
    try:
        with ipaldap.LDAPClient(ca_ldap_url,
                                start_tls=True,
                                force_schema_updates=False) as connection:
            connection.simple_bind(DN(('cn', 'Directory Manager')),
                                   config.dirman_password)
            rschema = connection.schema
            result = rschema.get_obj(ldap.schema.models.ObjectClass,
                                     objectclass)
    except Exception:
        root_logger.critical(
            'CA DS schema check failed. Make sure the PKI service on the '
            'remote master is operational.')
        raise
    if result:
        root_logger.debug('Check OK')
    else:
        root_logger.critical(
            'The master CA directory server does not have necessary schema. '
            'Please copy the following script to all CA masters and run it '
            'on them: %s\n'
            'If you are certain that this is a false positive, use '
            '--skip-schema-check.',
                os.path.join(ipautil.SHARE_DIR, 'copy-schema-to-ca.py'))
        exit('IPA schema missing on master CA directory server')


def install_replica_ca(config, postinstall=False, ra_p12=None):
    """
    Install a CA on a replica.

    There are two modes of doing this controlled:
      - While the replica is being installed
      - Post-replica installation

    config is a ReplicaConfig object

    Returns a tuple of the CA and CADS instances
    """
    cafile = config.dir + "/cacert.p12"

    if not ipautil.file_exists(cafile):
        # Replica of old "self-signed" master - skip installing CA
        return None

    ca = CAInstance(config.realm_name, certs.NSS_DIR)
    ca.dm_password = config.dirman_password
    ca.subject_base = config.subject_base

    if not config.setup_ca:
        # We aren't configuring the CA in this step but we still need
        # a minimum amount of information on the CA for this IPA install.
        return ca

    if ca.is_installed():
        sys.exit("A CA is already configured on this system.")

    if postinstall:
        # If installing this afterward the Apache NSS database already
        # exists, don't remove it.
        ca.create_ra_agent_db = False
    ca.configure_instance(config.host_name,
                          config.dirman_password, config.dirman_password,
                          pkcs12_info=(cafile,), ra_p12=ra_p12,
                          master_host=config.master_host_name,
                          master_replication_port=config.ca_ds_port,
                          subject_base=config.subject_base)

    # Restart httpd since we changed it's config and added ipa-pki-proxy.conf
    # Without the restart, CA service status check would fail due to missing
    # proxy
    if postinstall:
        services.knownservices.httpd.restart()


    # The dogtag DS instance needs to be restarted after installation.
    # The procedure for this is: stop dogtag, stop DS, start DS, start
    # dogtag
    #
    #
    # The service_name trickery is due to the service naming we do
    # internally. In the case of the dogtag DS the name doesn't match the
    # unix service.

    service.print_msg("Restarting the directory and certificate servers")
    ca.stop('pki-tomcat')

    services.knownservices.dirsrv.restart()

    ca.start('pki-tomcat')

    return ca


def backup_config():
    """
    Create a backup copy of CS.cfg
    """
    path = paths.CA_CS_CFG_PATH
    if services.knownservices['pki_tomcatd'].is_running('pki-tomcat'):
        raise RuntimeError(
            "Dogtag must be stopped when creating backup of %s" % path)
    shutil.copy(path, path + '.ipabkp')

def update_people_entry(dercert):
    """
    Update the userCerticate for an entry in the dogtag ou=People. This
    is needed when a certificate is renewed.

    dercert: An X509.3 certificate in DER format

    Logging is done via syslog

    Returns True or False
    """
    base_dn = DN(('o', 'ipaca'))
    serial_number = x509.get_serial_number(dercert, datatype=x509.DER)
    subject = x509.get_subject(dercert, datatype=x509.DER)
    issuer = x509.get_issuer(dercert, datatype=x509.DER)

    attempts = 0
    server_id = installutils.realm_to_serverid(api.env.realm)
    dogtag_uri = 'ldapi://%%2fvar%%2frun%%2fslapd-%s.socket' % server_id
    updated = False

    while attempts < 10:
        conn = None
        try:
            conn = ldap2.ldap2(api, ldap_uri=dogtag_uri)
            conn.connect(autobind=True)

            db_filter = conn.combine_filters(
                [
                    conn.make_filter({'objectClass': 'inetOrgPerson'}),
                    conn.make_filter(
                        {'description': ';%s;%s' % (issuer, subject)},
                        exact=False, trailing_wildcard=False),
                ],
                conn.MATCH_ALL)
            try:
                entries = conn.get_entries(base_dn, conn.SCOPE_SUBTREE, db_filter)
            except errors.NotFound:
                entries = []

            updated = True

            for entry in entries:
                syslog.syslog(
                    syslog.LOG_NOTICE, 'Updating entry %s' % str(entry.dn))

                try:
                    entry['usercertificate'].append(dercert)
                    entry['description'] = '2;%d;%s;%s' % (
                        serial_number, issuer, subject)

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
                'Connection to %s failed, sleeping 30s' % dogtag_uri)
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

def ensure_ldap_profiles_container():
    ensure_entry(
        DN(('ou', 'certificateProfiles'), ('ou', 'ca'), ('o', 'ipaca')),
        objectclass=['top', 'organizationalUnit'],
        ou=['certificateProfiles'],
    )


def ensure_entry(dn, **attrs):
    server_id = installutils.realm_to_serverid(api.env.realm)
    dogtag_uri = 'ldapi://%%2fvar%%2frun%%2fslapd-%s.socket' % server_id

    conn = ldap2.ldap2(api, ldap_uri=dogtag_uri)
    if not conn.isconnected():
        conn.connect(autobind=True)

    try:
        conn.get_entry(dn)
    except errors.NotFound:
        # entry doesn't exist; add it
        entry = conn.make_entry(dn, **attrs)
        conn.add_entry(entry)

    conn.disconnect()


def configure_profiles_acl():
    """Allow the Certificate Manager Agents group to modify profiles."""
    server_id = installutils.realm_to_serverid(api.env.realm)
    dogtag_uri = 'ldapi://%%2fvar%%2frun%%2fslapd-%s.socket' % server_id
    updated = False

    dn = DN(('cn', 'aclResources'), ('o', 'ipaca'))
    new_rules = [
        'certServer.profile.configuration:read,modify:allow (read,modify) '
        'group="Certificate Manager Agents":'
        'Certificate Manager agents may modify (create/update/delete) and read profiles',

        'certServer.ca.account:login,logout:allow (login,logout) '
        'user="anybody":Anybody can login and logout',
    ]

    conn = ldap2.ldap2(api, ldap_uri=dogtag_uri)
    if not conn.isconnected():
        conn.connect(autobind=True)
    cur_rules = conn.get_entry(dn).get('resourceACLS', [])
    add_rules = [rule for rule in new_rules if rule not in cur_rules]
    if add_rules:
        conn.conn.modify_s(str(dn), [(ldap.MOD_ADD, 'resourceACLS', add_rules)])
        updated = True

    conn.disconnect()
    return updated


def __get_profile_config(profile_id):
    sub_dict = dict(
        DOMAIN=ipautil.format_netloc(api.env.domain),
        IPA_CA_RECORD=ipalib.constants.IPA_CA_RECORD,
        CRL_ISSUER='CN=Certificate Authority,o=ipaca',
        SUBJECT_DN_O=dsinstance.DsInstance().find_subject_base(),
    )
    return ipautil.template_file(
        '/usr/share/ipa/profiles/{}.cfg'.format(profile_id), sub_dict)

def import_included_profiles():
    server_id = installutils.realm_to_serverid(api.env.realm)
    dogtag_uri = 'ldapi://%%2fvar%%2frun%%2fslapd-%s.socket' % server_id
    conn = ldap2.ldap2(api, ldap_uri=dogtag_uri)
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

    api.Backend.ra_certprofile._read_password()
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
            root_logger.info("Imported profile '%s'", profile_id)

    api.Backend.ra_certprofile.override_port = None
    conn.disconnect()


def repair_profile_caIPAserviceCert():
    """
    A regression caused replica installation to replace the FreeIPA
    version of caIPAserviceCert with the version shipped by Dogtag.

    This function detects and repairs occurrences of this problem.

    """
    api.Backend.ra_certprofile._read_password()
    api.Backend.ra_certprofile.override_port = 8443

    profile_id = 'caIPAserviceCert'

    with api.Backend.ra_certprofile as profile_api:
        try:
            cur_config = profile_api.read_profile(profile_id).splitlines()
        except errors.RemoteRetrieveError as e:
            # no profile there to check/repair
            api.Backend.ra_certprofile.override_port = None
            return

    indicators = [
        "policyset.serverCertSet.1.default.params.name="
            "CN=$request.req_subject_name.cn$, OU=pki-ipa, O=IPA ",
        "policyset.serverCertSet.9.default.params.crlDistPointsPointName_0="
            "https://ipa.example.com/ipa/crl/MasterCRL.bin",
        ]
    need_repair = all(l in cur_config for l in indicators)

    if need_repair:
        root_logger.debug(
            "Detected that profile '{}' has been replaced with "
            "incorrect version; begin repair.".format(profile_id))
        _create_dogtag_profile(
            profile_id, __get_profile_config(profile_id), overwrite=True)
        root_logger.debug("Repair of profile '{}' complete.".format(profile_id))

    api.Backend.ra_certprofile.override_port = None


def migrate_profiles_to_ldap():
    """Migrate profiles from filesystem to LDAP.

    This must be run *after* switching to the LDAPProfileSubsystem
    and restarting the CA.

    The profile might already exist, e.g. if a replica was already
    upgraded, so this case is ignored.

    """
    ensure_ldap_profiles_container()

    api.Backend.ra_certprofile._read_password()
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
            root_logger.info("No file for profile '%s'; skipping", profile_id)
            continue
        filename = match.group(1)

        match = re.search(
            r'^profile\.{}\.class_id=(\S*)'.format(profile_id),
            cs_cfg, re.MULTILINE
        )
        if match is None:
            root_logger.info("No class_id for profile '%s'; skipping", profile_id)
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
            root_logger.info("Profile '%s' successfully migrated to LDAP",
                             profile_id)
        except errors.RemoteRetrieveError as e:
            root_logger.debug("Error migrating '{}': {}".format(
                profile_id, e))

            # profile already exists
            if overwrite:
                try:
                    profile_api.disable_profile(profile_id)
                except errors.RemoteRetrieveError:
                    root_logger.debug(
                        "Failed to disable profile '%s' "
                        "(it is probably already disabled)")
                profile_api.update_profile(profile_id, profile_data)

        # enable the profile
        try:
            profile_api.enable_profile(profile_id)
        except errors.RemoteRetrieveError:
            root_logger.debug(
                "Failed to enable profile '%s' "
                "(it is probably already enabled)")


def ensure_default_caacl():
    """Add the default CA ACL if missing."""
    is_already_connected = api.Backend.ldap2.isconnected()
    if not is_already_connected:
        try:
            api.Backend.ldap2.connect(autobind=True)
        except errors.PublicError as e:
            root_logger.error("Cannot connect to LDAP to add CA ACLs: %s", e)
            return

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

    if not is_already_connected:
        api.Backend.ldap2.disconnect()


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


if __name__ == "__main__":
    standard_logging_setup("install.log")
    ds = dsinstance.DsInstance()

    ca = CAInstance("EXAMPLE.COM", paths.HTTPD_ALIAS_DIR)
    ca.configure_instance("catest.example.com", "password", "password")
