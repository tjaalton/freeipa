#
# Copyright (C) 2015  FreeIPA Contributors see COPYING for license
#

"""
Module provides tests which testing ability of various subsystems to be
installed.
"""

from __future__ import absolute_import

import os
import re
import textwrap
import time
from datetime import datetime, timedelta

import pytest
from cryptography.hazmat.primitives import hashes

from ipalib import x509
from ipalib.constants import DOMAIN_LEVEL_0
from ipaplatform.constants import constants
from ipaplatform.osinfo import osinfo
from ipaplatform.paths import paths
from ipaplatform.tasks import tasks as platformtasks
from ipapython import ipautil
from ipatests.pytest_ipa.integration import tasks
from ipatests.pytest_ipa.integration.env_config import get_global_config
from ipatests.test_integration.base import IntegrationTest
from ipatests.test_integration.test_caless import CALessBase, ipa_certs_cleanup
from ipaplatform import services

config = get_global_config()


def create_broken_resolv_conf(master):
    # Force a broken resolv.conf to simulate a bad response to
    # reverse zone lookups
    master.run_command([
        '/bin/mv',
        paths.RESOLV_CONF,
        '%s.sav' % paths.RESOLV_CONF
    ])

    contents = "# Set as broken by ipatests\nnameserver 127.0.0.2\n"
    master.put_file_contents(paths.RESOLV_CONF, contents)


def restore_resolv_conf(master):
    if os.path.exists('%s.sav' % paths.RESOLV_CONF):
        master.run_command([
            '/bin/mv',
            '%s.sav' % paths.RESOLV_CONF,
            paths.RESOLV_CONF
        ])


def server_install_setup(func):
    def wrapped(*args):
        master = args[0].master
        create_broken_resolv_conf(master)
        try:
            func(*args)
        finally:
            tasks.uninstall_master(master, clean=False)
            restore_resolv_conf(master)
            ipa_certs_cleanup(master)
    return wrapped


class InstallTestBase1(IntegrationTest):

    num_replicas = 3
    topology = 'star'

    @classmethod
    def install(cls, mh):
        tasks.install_master(cls.master, setup_dns=False)

    def test_replica0_ca_less_install(self):
        tasks.install_replica(self.master, self.replicas[0], setup_ca=False)

    def test_replica0_ipa_ca_install(self):
        tasks.install_ca(self.replicas[0])

    def test_replica0_ipa_kra_install(self):
        tasks.install_kra(self.replicas[0], first_instance=True)

    def test_replica0_ipa_dns_install(self):
        tasks.install_dns(self.replicas[0])

    def test_replica1_with_ca_install(self):
        tasks.install_replica(self.master, self.replicas[1], setup_ca=True)

    def test_replica1_ipa_kra_install(self):
        tasks.install_kra(self.replicas[1])

    def test_replica1_ipa_dns_install(self):
        tasks.install_dns(self.replicas[1])

    def test_replica2_with_ca_kra_install(self):
        tasks.install_replica(self.master, self.replicas[2], setup_ca=True,
                              setup_kra=True)

    def test_replica2_ipa_dns_install(self):
        tasks.install_dns(self.replicas[2])


class InstallTestBase2(IntegrationTest):

    num_replicas = 3
    topology = 'star'

    @classmethod
    def install(cls, mh):
        tasks.install_master(cls.master, setup_dns=False)

    def test_replica1_with_ca_dns_install(self):
        tasks.install_replica(self.master, self.replicas[1], setup_ca=True,
                              setup_dns=True)

    def test_replica1_ipa_kra_install(self):
        tasks.install_kra(self.replicas[1])

    def test_replica2_with_dns_install(self):
        tasks.install_replica(self.master, self.replicas[2], setup_ca=False,
                              setup_dns=True)

    def test_replica2_ipa_ca_install(self):
        tasks.install_ca(self.replicas[2])

    def test_replica2_ipa_kra_install(self):
        tasks.install_kra(self.replicas[2])


class ADTrustInstallTestBase(IntegrationTest):
    """
    Base test for builtin AD trust installation im combination with other
    components
    """
    num_replicas = 2
    topology = 'star'

    @classmethod
    def install(cls, mh):
        tasks.install_master(cls.master, setup_dns=False)

    def install_replica(self, replica, **kwargs):
        tasks.install_replica(self.master, replica, setup_adtrust=True,
                              **kwargs)

    def test_replica0_only_adtrust(self):
        self.install_replica(self.replicas[0], setup_ca=False)

    def test_replica1_all_components_adtrust(self):
        self.install_replica(self.replicas[1], setup_ca=True)


##
# Master X Replicas installation tests
##

class TestInstallWithCA1(InstallTestBase1):

    @classmethod
    def install(cls, mh):
        tasks.install_master(cls.master, setup_dns=False)

    @pytest.mark.skipif(config.domain_level == DOMAIN_LEVEL_0,
                        reason='does not work on DOMAIN_LEVEL_0 by design')
    def test_replica1_ipa_kra_install(self):
        super(TestInstallWithCA1, self).test_replica1_ipa_kra_install()

    @pytest.mark.skipif(config.domain_level == DOMAIN_LEVEL_0,
                        reason='does not work on DOMAIN_LEVEL_0 by design')
    def test_replica2_with_ca_kra_install(self):
        super(TestInstallWithCA1, self).test_replica2_with_ca_kra_install()

    @pytest.mark.skipif(config.domain_level == DOMAIN_LEVEL_0,
                        reason='does not work on DOMAIN_LEVEL_0 by design')
    def test_replica2_ipa_dns_install(self):
        super(TestInstallWithCA1, self).test_replica2_ipa_dns_install()

    def test_install_with_bad_ldap_conf(self):
        """
        Test a client install with a non standard ldap.config
        https://pagure.io/freeipa/issue/7418
        """
        ldap_conf = paths.OPENLDAP_LDAP_CONF
        base_dn = self.master.domain.basedn
        client = self.replicas[0]
        tasks.uninstall_master(client)
        expected_msg1 = "contains deprecated and unsupported " \
                        "entries: HOST, PORT"
        file_backup = client.get_file_contents(ldap_conf, encoding='utf-8')
        constants = "URI ldaps://{}\nBASE {}\nHOST {}\nPORT 636".format(
            self.master.hostname, base_dn,
            self.master.hostname)
        modifications = "{}\n{}".format(file_backup, constants)
        client.put_file_contents(paths.OPENLDAP_LDAP_CONF, modifications)
        result = client.run_command(['ipa-client-install', '-U',
                                     '--domain', client.domain.name,
                                     '--realm', client.domain.realm,
                                     '-p', client.config.admin_name,
                                     '-w', client.config.admin_password,
                                     '--server', self.master.hostname],
                                    raiseonerr=False)
        assert expected_msg1 in result.stderr_text
        client.put_file_contents(ldap_conf, file_backup)


class TestInstallWithCA2(InstallTestBase2):

    @classmethod
    def install(cls, mh):
        tasks.install_master(cls.master, setup_dns=False)

    @pytest.mark.skipif(config.domain_level == DOMAIN_LEVEL_0,
                        reason='does not work on DOMAIN_LEVEL_0 by design')
    def test_replica1_ipa_kra_install(self):
        super(TestInstallWithCA2, self).test_replica1_ipa_kra_install()

    @pytest.mark.skipif(config.domain_level == DOMAIN_LEVEL_0,
                        reason='does not work on DOMAIN_LEVEL_0 by design')
    def test_replica2_ipa_kra_install(self):
        super(TestInstallWithCA2, self).test_replica2_ipa_kra_install()


class TestInstallCA(IntegrationTest):
    """
    Tests for CA installation on a replica
    """

    num_replicas = 2

    @classmethod
    def install(cls, mh):
        tasks.install_master(cls.master, setup_dns=False)

    def test_replica_ca_install_with_no_host_dns(self):
        """
        Test for ipa-ca-install --no-host-dns on a replica
        """

        tasks.install_replica(self.master, self.replicas[0], setup_ca=False)
        tasks.install_ca(self.replicas[0], extra_args=["--no-host-dns"])

    def test_replica_ca_install_with_skip_schema_check(self):
        """
        Test for ipa-ca-install --skip-schema-check on a replica
        """

        tasks.install_replica(self.master, self.replicas[1], setup_ca=False)
        tasks.install_ca(self.replicas[1], extra_args=["--skip-schema-check"])

    def test_certmonger_reads_token_HSM(self):
        """Test if certmonger reads the token in HSM

        This is to ensure added HSM support for FreeIPA. This test adds
        certificate with sofhsm token and checks if certmonger is tracking
        it.

        related : https://pagure.io/certmonger/issue/125
        """
        test_service = 'test/%s' % self.master.hostname
        pkcs_passwd = 'Secret123'
        pin = '123456'
        noisefile = '/tmp/noisefile'
        self.master.put_file_contents(noisefile, os.urandom(64))

        tasks.kinit_admin(self.master)
        tasks.install_dns(self.master)
        self.master.run_command(['ipa', 'service-add', test_service])

        # create a csr
        cmd_args = ['certutil', '-d', paths.NSS_DB_DIR, '-R', '-a',
                    '-o', '/root/ipa.csr',
                    '-s', "CN=%s" % self.master.hostname,
                    '-z', noisefile]
        self.master.run_command(cmd_args)

        # request certificate
        cmd_args = ['ipa', 'cert-request', '--principal', test_service,
                    '--certificate-out', '/root/test.pem', '/root/ipa.csr']
        self.master.run_command(cmd_args)

        # adding trust flag
        cmd_args = ['certutil', '-A', '-d', paths.NSS_DB_DIR, '-n',
                    'test', '-a', '-i', '/root/test.pem', '-t', 'u,u,u']
        self.master.run_command(cmd_args)

        # export pkcs12 file
        cmd_args = ['pk12util', '-o', '/root/test.p12',
                    '-d', paths.NSS_DB_DIR, '-n', 'test', '-W', pkcs_passwd]
        self.master.run_command(cmd_args)

        # add softhsm lib
        cmd_args = ['modutil', '-dbdir', paths.NSS_DB_DIR, '-add',
                    'softhsm', '-libfile', '/usr/lib64/softhsm/libsofthsm.so']
        self.master.run_command(cmd_args, stdin_text="\n\n")

        # create a token
        cmd_args = ['softhsm2-util', '--init-token', '--label', 'test',
                    '--pin', pin, '--so-pin', pin, '--free']
        self.master.run_command(cmd_args)

        self.master.run_command(['softhsm2-util', '--show-slots'])

        cmd_args = ['certutil', '-F', '-d', paths.NSS_DB_DIR, '-n', 'test']
        self.master.run_command(cmd_args)

        cmd_args = ['pk12util', '-i', '/root/test.p12',
                    '-d', paths.NSS_DB_DIR, '-h', 'test',
                    '-W', pkcs_passwd, '-K', pin]
        self.master.run_command(cmd_args)

        cmd_args = ['certutil', '-A', '-d', paths.NSS_DB_DIR, '-n', 'IPA CA',
                    '-t', 'CT,,', '-a', '-i', paths.IPA_CA_CRT]
        self.master.run_command(cmd_args)

        # validate the certificate
        self.master.put_file_contents('/root/pinfile', pin)
        cmd_args = ['certutil', '-V', '-u', 'V', '-e', '-d', paths.NSS_DB_DIR,
                    '-h', 'test', '-n', 'test:test', '-f', '/root/pinfile']
        result = self.master.run_command(cmd_args)
        assert 'certificate is valid' in result.stdout_text

        # add certificate tracking to certmonger
        cmd_args = ['ipa-getcert', 'start-tracking', '-d', paths.NSS_DB_DIR,
                    '-n', 'test', '-t', 'test', '-P', pin,
                    '-K', test_service]
        result = self.master.run_command(cmd_args)
        request_id = re.findall(r'\d+', result.stdout_text)

        # check if certificate is tracked by certmonger
        status = tasks.wait_for_request(self.master, request_id[0], 300)
        assert status == "MONITORING"

        # ensure if key and token are re-usable
        cmd_args = ['getcert', 'resubmit', '-i', request_id[0]]
        self.master.run_command(cmd_args)

        status = tasks.wait_for_request(self.master, request_id[0], 300)
        assert status == "MONITORING"


class TestInstallWithCA_KRA1(InstallTestBase1):

    @classmethod
    def install(cls, mh):
        tasks.install_master(cls.master, setup_dns=False, setup_kra=True)

    def test_replica0_ipa_kra_install(self):
        tasks.install_kra(self.replicas[0], first_instance=False)


class TestInstallWithCA_KRA2(InstallTestBase2):

    @classmethod
    def install(cls, mh):
        tasks.install_master(cls.master, setup_dns=False, setup_kra=True)


class TestInstallWithCA_DNS1(InstallTestBase1):

    @classmethod
    def install(cls, mh):
        tasks.install_master(cls.master, setup_dns=True)

    @pytest.mark.skipif(config.domain_level == DOMAIN_LEVEL_0,
                        reason='does not work on DOMAIN_LEVEL_0 by design')
    def test_replica1_ipa_kra_install(self):
        super(TestInstallWithCA_DNS1, self).test_replica1_ipa_kra_install()

    @pytest.mark.skipif(config.domain_level == DOMAIN_LEVEL_0,
                        reason='does not work on DOMAIN_LEVEL_0 by design')
    def test_replica2_with_ca_kra_install(self):
        super(TestInstallWithCA_DNS1, self).test_replica2_with_ca_kra_install()

    @pytest.mark.skipif(config.domain_level == DOMAIN_LEVEL_0,
                        reason='does not work on DOMAIN_LEVEL_0 by design')
    def test_replica2_ipa_dns_install(self):
        super(TestInstallWithCA_DNS1, self).test_replica2_ipa_dns_install()


class TestInstallWithCA_DNS2(InstallTestBase2):

    @classmethod
    def install(cls, mh):
        tasks.install_master(cls.master, setup_dns=True)

    @pytest.mark.skipif(config.domain_level == DOMAIN_LEVEL_0,
                        reason='does not work on DOMAIN_LEVEL_0 by design')
    def test_replica1_ipa_kra_install(self):
        super(TestInstallWithCA_DNS2, self).test_replica1_ipa_kra_install()

    @pytest.mark.skipif(config.domain_level == DOMAIN_LEVEL_0,
                        reason='does not work on DOMAIN_LEVEL_0 by design')
    def test_replica2_ipa_kra_install(self):
        super(TestInstallWithCA_DNS2, self).test_replica2_ipa_kra_install()


class TestInstallWithCA_DNS3(CALessBase):
    """
    Test an install with a bad DNS resolver configured to force a
    timeout trying to verify the existing zones. In the case of a reverse
    zone it is skipped unless --allow-zone-overlap is set regardless of
    the value of --auto-reverse. Confirm that --allow-zone-overlap
    lets the reverse zone be created.

    ticket 7239
    """

    @server_install_setup
    def test_number_of_zones(self):
        """There should be two zones: one forward, one reverse"""

        self.create_pkcs12('ca1/server')
        self.prepare_cacert('ca1')

        self.install_server(extra_args=['--allow-zone-overlap'])

        result = self.master.run_command([
            'ipa', 'dnszone-find'])

        assert "in-addr.arpa." in result.stdout_text

        assert "returned 2" in result.stdout_text


class TestInstallWithCA_DNS4(CALessBase):
    """
    Test an install with a bad DNS resolver configured to force a
    timeout trying to verify the existing zones. In the case of a reverse
    zone it is skipped unless --allow-zone-overlap is set regardless of
    the value of --auto-reverse. Confirm that without --allow-reverse-zone
    only the forward zone is created.

    ticket 7239
    """

    @server_install_setup
    def test_number_of_zones(self):
        """There should be one zone, a forward because rev timed-out"""

        self.create_pkcs12('ca1/server')
        self.prepare_cacert('ca1')

        # no zone overlap by default
        self.install_server()

        result = self.master.run_command([
            'ipa', 'dnszone-find'])

        assert "in-addr.arpa." not in result.stdout_text

        assert "returned 1" in result.stdout_text


@pytest.mark.cs_acceptance
class TestInstallWithCA_KRA_DNS1(InstallTestBase1):

    @classmethod
    def install(cls, mh):
        tasks.install_master(cls.master, setup_dns=True, setup_kra=True)

    def test_replica0_ipa_kra_install(self):
        tasks.install_kra(self.replicas[0], first_instance=False)


class TestInstallWithCA_KRA_DNS2(InstallTestBase2):

    @classmethod
    def install(cls, mh):
        tasks.install_master(cls.master, setup_dns=True, setup_kra=True)


class TestADTrustInstall(ADTrustInstallTestBase):
    """
    Tests built-in AD trust installation in various combinations (see the base
    class for more details) against plain IPA master (no DNS, no KRA, no AD
    trust)
    """


class TestADTrustInstallWithDNS_KRA_ADTrust(ADTrustInstallTestBase):
    """
    Tests built-in AD trust installation in various combinations (see the base
    class for more details) against fully equipped (DNS, CA, KRA, ADtrust)
    master. Additional two test cases were added to test interplay including
    KRA installer
    """

    @classmethod
    def install(cls, mh):
        tasks.install_master(cls.master, setup_dns=True, setup_kra=True,
                             setup_adtrust=True)

    def test_replica1_all_components_adtrust(self):
        self.install_replica(self.replicas[1], setup_ca=True, setup_kra=True)


def get_pki_tomcatd_pid(host):
    pid = ''
    cmd = host.run_command(['systemctl', 'status', 'pki-tomcatd@pki-tomcat'])
    for line in cmd.stdout_text.split('\n'):
        if "Main PID" in line:
            pid = line.split()[2]
            break
    return(pid)


def get_ipa_services_pids(host):
    ipa_services_name = [
        "krb5kdc", "kadmin", "named", "httpd", "ipa-custodia",
        "pki_tomcatd", "ipa-dnskeysyncd"
    ]
    pids_of_ipa_services = {}
    for name in ipa_services_name:
        service_name = services.knownservices[name].systemd_name
        result = host.run_command(
            ["systemctl", "-p", "MainPID", "--value", "show", service_name]
        )
        pids_of_ipa_services[service_name] = int(result.stdout_text.strip())
    return pids_of_ipa_services


##
# Rest of master installation tests
##

class TestInstallMaster(IntegrationTest):

    num_replicas = 0

    @classmethod
    def install(cls, mh):
        pass

    def test_install_master(self):
        tasks.install_master(self.master, setup_dns=False)

    def test_install_kra(self):
        tasks.install_kra(self.master, first_instance=True)

    def test_install_dns(self):
        tasks.install_dns(
            self.master,
            extra_args=['--dnssec-master', '--no-dnssec-validation']
        )

    def test_ipactl_restart_pki_tomcat(self):
        """ Test if ipactl restart restarts the pki-tomcatd

        Wrong logic was triggering the start instead of restart
        for pki-tomcatd. This test validates that restart
        called on pki-tomcat properly.

        related ticket : https://pagure.io/freeipa/issue/7927
        """
        # get process id of pki-tomcatd
        pki_pid = get_pki_tomcatd_pid(self.master)

        # check if pki-tomcad restarted
        cmd = self.master.run_command(['ipactl', 'restart'])
        assert "Restarting pki-tomcatd Service" in cmd.stdout_text

        # check if pid for pki-tomcad changed
        pki_pid_after_restart = get_pki_tomcatd_pid(self.master)
        assert pki_pid != pki_pid_after_restart

        # check if pki-tomcad restarted
        cmd = self.master.run_command(['ipactl', 'restart'])
        assert "Restarting pki-tomcatd Service" in cmd.stdout_text

        # check if pid for pki-tomcad changed
        pki_pid_after_restart_2 = get_pki_tomcatd_pid(self.master)
        assert pki_pid_after_restart != pki_pid_after_restart_2

    def test_ipactl_scenario_check(self):
        """ Test if ipactl starts services stopped by systemctl
        This will first check if all services are running then it will stop
        few service. After that it will restart all services and then check
        the status and pid of services.It will also compare pid after ipactl
        start and restart in case of start it will remain unchanged on the
        other hand in case of restart it will change.
        """
        # listing all services
        services = [
            "Directory", "krb5kdc", "kadmin", "named", "httpd", "ipa-custodia",
            "pki-tomcatd", "ipa-otpd", "ipa-dnskeysyncd"
        ]

        # checking the service status
        cmd = self.master.run_command(['ipactl', 'status'])
        for service in services:
            assert f"{service} Service: RUNNING" in cmd.stdout_text

        # stopping few services
        service_stop = ["krb5kdc", "kadmin", "httpd"]
        for service in service_stop:
            self.master.run_command(['systemctl', 'stop', service])

        # checking service status
        service_start = [
            svcs for svcs in services if svcs not in service_stop
        ]
        cmd = self.master.run_command(['ipactl', 'status'])
        for service in service_start:
            assert f"{service} Service: RUNNING" in cmd.stdout_text
        for service in service_stop:
            assert f'{service} Service: STOPPED' in cmd.stdout_text

        # starting all services again
        self.master.run_command(['ipactl', 'start'])

        # checking service status
        cmd = self.master.run_command(['ipactl', 'status'])
        for service in services:
            assert f"{service} Service: RUNNING" in cmd.stdout_text

        # get process id of services
        ipa_services_pids = get_ipa_services_pids(self.master)

        # restarting all services again
        self.master.run_command(['ipactl', 'restart'])

        # checking service status
        cmd = self.master.run_command(['ipactl', 'status'])
        for service in services:
            assert f"{service} Service: RUNNING" in cmd.stdout_text

        # check if pid for services are different
        svcs_pids_after_restart = get_ipa_services_pids(self.master)
        assert ipa_services_pids != svcs_pids_after_restart

        # starting all services again
        self.master.run_command(['ipactl', 'start'])

        # checking service status
        cmd = self.master.run_command(['ipactl', 'status'])
        for service in services:
            assert f"{service} Service: RUNNING" in cmd.stdout_text

        # check if pid for services are same
        svcs_pids_after_start = get_ipa_services_pids(self.master)
        assert svcs_pids_after_restart == svcs_pids_after_start

    def test_WSGI_worker_process(self):
        """ Test if WSGI worker process count is set to 4

        related ticket : https://pagure.io/freeipa/issue/7587
        """
        # check process count in httpd conf file i.e expected string
        exp = b'WSGIDaemonProcess ipa processes=%d' % constants.WSGI_PROCESSES
        httpd_conf = self.master.get_file_contents(paths.HTTPD_IPA_CONF)
        assert exp in httpd_conf

        # check the process count
        cmd = self.master.run_command('ps -eF')
        wsgi_count = cmd.stdout_text.count('wsgi:ipa')
        assert constants.WSGI_PROCESSES == wsgi_count

    def test_error_for_yubikey(self):
        """ Test error when yubikey hardware not present

        In order to work with IPA and Yubikey, libyubikey is required.
        Before the fix, if yubikey added without having packages, it used to
        result in traceback. Now it the exception is handeled properly.
        It needs Yubikey hardware to make command successfull. This test
        just check of proper error thrown when hardware is not attached.

        related ticket : https://pagure.io/freeipa/issue/6979
        """
        # try to add yubikey to the user
        args = ['ipa', 'otptoken-add-yubikey', '--owner=admin']
        cmd = self.master.run_command(args, raiseonerr=False)
        assert cmd.returncode != 0
        exp_str = ("ipa: ERROR: No YubiKey found")
        assert exp_str in cmd.stderr_text

    def test_pki_certs(self):
        certs, keys = tasks.certutil_certs_keys(
            self.master,
            paths.PKI_TOMCAT_ALIAS_DIR,
            paths.PKI_TOMCAT_ALIAS_PWDFILE_TXT
        )

        expected_certs = {
            # CA
            'caSigningCert cert-pki-ca': 'CTu,Cu,Cu',
            'ocspSigningCert cert-pki-ca': 'u,u,u',
            'subsystemCert cert-pki-ca': 'u,u,u',
            'auditSigningCert cert-pki-ca': 'u,u,Pu',  # why P?
            # KRA
            'transportCert cert-pki-kra': 'u,u,u',
            'storageCert cert-pki-kra': 'u,u,u',
            'auditSigningCert cert-pki-kra': 'u,u,Pu',
            # server
            'Server-Cert cert-pki-ca': 'u,u,u',
        }
        assert certs == expected_certs
        assert len(certs) == len(keys)

        for nickname in sorted(certs):
            cert = tasks.certutil_fetch_cert(
                self.master,
                paths.PKI_TOMCAT_ALIAS_DIR,
                paths.PKI_TOMCAT_ALIAS_PWDFILE_TXT,
                nickname
            )
            key_size = cert.public_key().key_size
            if nickname == 'caSigningCert cert-pki-ca':
                assert key_size == 3072
            else:
                assert key_size == 2048
            assert cert.signature_hash_algorithm.name == hashes.SHA256.name

    def test_p11_kit_softhsm2(self):
        # check that p11-kit-proxy does not inject SoftHSM2
        result = self.master.run_command([
            "modutil", "-dbdir", paths.PKI_TOMCAT_ALIAS_DIR, "-list"
        ])
        assert "softhsm" not in result.stdout_text.lower()
        assert "opendnssec" not in result.stdout_text.lower()

    @pytest.mark.skipif(
        not platformtasks.is_selinux_enabled(),
        reason="Test needs SELinux enabled")
    def test_selinux_avcs(self):
        # Use journalctl instead of ausearch. The ausearch command is not
        # installed by default and journalctl gives us all AVCs.
        result = self.master.run_command([
            "journalctl", "--full", "--grep=AVC", "--since=yesterday"
        ])
        avcs = list(
            line.strip() for line in result.stdout_text.split('\n')
            if "AVC avc:" in line
        )
        if avcs:
            print('\n'.join(avcs))
            # Use expected failure until all SELinux violations are fixed
            pytest.xfail("{} AVCs found".format(len(avcs)))

    def test_file_permissions(self):
        args = [
            "rpm", "-V",
            "python3-ipaclient",
            "python3-ipalib",
            "python3-ipaserver"
        ]

        if osinfo.id == 'fedora':
            args.extend([
                "freeipa-client",
                "freeipa-client-common",
                "freeipa-common",
                "freeipa-server",
                "freeipa-server-common",
                "freeipa-server-dns",
                "freeipa-server-trust-ad"
            ])
        else:
            args.extend([
                "ipa-client",
                "ipa-client-common",
                "ipa-common",
                "ipa-server",
                "ipa-server-common",
                "ipa-server-dns"
            ])

        result = self.master.run_command(args, raiseonerr=False)
        if result.returncode != 0:
            # Check the mode errors
            mode_warnings = re.findall(
                r"^.M.......  [cdglr ]+ (?P<filename>.*)$",
                result.stdout_text, re.MULTILINE)
            msg = "rpm -V found mode issues for the following files: {}"
            assert mode_warnings == [], msg.format(mode_warnings)
            # Check the owner errors
            user_warnings = re.findall(
                r"^.....U...  [cdglr ]+ (?P<filename>.*)$",
                result.stdout_text, re.MULTILINE)
            msg = "rpm -V found ownership issues for the following files: {}"
            assert user_warnings == [], msg.format(user_warnings)
            # Check the group errors
            group_warnings = re.findall(
                r"^......G..  [cdglr ]+ (?P<filename>.*)$",
                result.stdout_text, re.MULTILINE)
            msg = "rpm -V found group issues for the following files: {}"
            assert group_warnings == [], msg.format(group_warnings)


class TestInstallMasterKRA(IntegrationTest):

    num_replicas = 0

    @classmethod
    def install(cls, mh):
        pass

    def test_install_master(self):
        tasks.install_master(self.master, setup_dns=False, setup_kra=True)

    def test_install_dns(self):
        tasks.install_dns(self.master)


class TestInstallMasterDNS(IntegrationTest):

    num_replicas = 0

    @classmethod
    def install(cls, mh):
        pass

    def test_install_master(self):
        tasks.install_master(
            self.master,
            setup_dns=True,
            extra_args=['--zonemgr', 'me@example.org'],
        )

    def test_server_install_lock_bind_recursion(self):
        """Test if server installer lock Bind9 recursion

        This test is to check if recursion can be configured.
        It checks if newly added file /etc/named/ipa-ext.conf
        exists and /etc/named.conf should not have
        'allow-recursion { any; };'. It also checks if ipa-backup
        command backup the /etc/named/ipa-ext.conf file as well

        related : https://pagure.io/freeipa/issue/8079
        """
        # check of /etc/named/ipa-ext.conf exist
        assert self.master.transport.file_exists(paths.NAMED_CUSTOM_CONFIG)

        # check if /etc/named.conf does not contain 'allow-recursion { any; };'
        string_to_check = 'allow-recursion { any; };'
        named_contents = self.master.get_file_contents(paths.NAMED_CONF,
                                                       encoding='utf-8')
        assert string_to_check not in named_contents

        # check if ipa-backup command backups the /etc/named/ipa-ext.conf
        result = self.master.run_command(['ipa-backup', '-v'])
        assert paths.NAMED_CUSTOM_CONFIG in result.stderr_text

    def test_install_kra(self):
        tasks.install_kra(self.master, first_instance=True)


class TestInstallMasterDNSRepeatedly(IntegrationTest):
    """ Test that a repeated installation of the primary with DNS enabled
    will lead to a already installed message and not in "DNS zone X
    already exists in DNS" in check_zone_overlap.
    The error is only occuring if domain is set explicitly in the command
    line installer as check_zone_overlap is used in the domain_name
    validator.
    """

    num_replicas = 0

    @classmethod
    def install(cls, mh):
        tasks.install_master(cls.master, setup_dns=True)

    def test_install_master_releatedly(self):
        cmd = tasks.install_master(self.master,
                                   setup_dns=True,
                                   raiseonerr=False)
        exp_str = ("already exists in DNS")
        assert (exp_str not in cmd.stderr_text and cmd.returncode != 2)


class TestInstallMasterReservedIPasForwarder(IntegrationTest):
    """Test to check if IANA reserved IP doesn't accepted as DNS forwarder

    IANA reserved IP address can not be used as a forwarder.
    This test checks if ipa server installation throws an error when
    0.0.0.0 is specified as forwarder IP address.

    related ticket: https://pagure.io/freeipa/issue/6894
    """

    def test_reserved_ip_as_forwarder(self):
        args = [
            'ipa-server-install',
            '-n', self.master.domain.name,
            '-r', self.master.domain.realm,
            '-p', self.master.config.dirman_password,
            '-a', self.master.config.admin_password,
            '--setup-dns',
            '--forwarder', '0.0.0.0',
            '--auto-reverse']
        cmd = self.master.run_command(args, raiseonerr=False)
        assert cmd.returncode == 2
        exp_str = ("error: option --forwarder: invalid IP address 0.0.0.0: "
                   "cannot use IANA reserved IP address 0.0.0.0")
        assert exp_str in cmd.stderr_text

        server_install_options = (
                "yes\n"
                "{hostname}\n"
                "{dmname}\n\n"
                "{dm_pass}\n{dm_pass}"
                "\n{admin_pass}\n{admin_pass}\n"
                "yes\nyes\n0.0.0.0\n".format(
                    dm_pass=self.master.config.dirman_password,
                    admin_pass=self.master.config.admin_password,
                    dmname=self.master.domain.name,
                    hostname=self.master.hostname))

        cmd = self.master.run_command(['ipa-server-install'],
                                      stdin_text=server_install_options,
                                      raiseonerr=False)
        exp_str = ("Invalid IP Address 0.0.0.0: cannot use IANA reserved "
                   "IP address 0.0.0.0")
        assert exp_str in cmd.stdout_text


class TestKRAinstallAfterCertRenew(IntegrationTest):
    """ Test KRA installtion after ca agent cert renewal

    KRA installation was failing after ca-agent cert gets renewed.
    This test checks if the symptoms no longer exist.

    related ticket: https://pagure.io/freeipa/issue/7288
    """

    def test_KRA_install_after_cert_renew(self):

        tasks.install_master(self.master)

        # get ca-agent cert and load as pem
        dm_pass = self.master.config.dirman_password
        admin_pass = self.master.config.admin_password
        args = [paths.OPENSSL, "pkcs12", "-in",
                paths.DOGTAG_ADMIN_P12, "-nodes",
                "-passin", "pass:{}".format(dm_pass)]
        cmd = self.master.run_command(args)

        certs = x509.load_certificate_list(cmd.stdout_text.encode('utf-8'))

        # get expiry date of agent cert
        cert_expiry = certs[0].not_valid_after

        # move date to grace period so that certs get renewed
        self.master.run_command(['systemctl', 'stop', 'chronyd'])
        grace_date = cert_expiry - timedelta(days=10)
        grace_date = datetime.strftime(grace_date, "%Y-%m-%d %H:%M:%S")
        self.master.run_command(['date', '-s', grace_date])

        # get the count of certs track by certmonger
        cmd = self.master.run_command(['getcert', 'list'])
        cert_count = cmd.stdout_text.count('Request ID')
        timeout = 600
        count = 0
        start = time.time()
        # wait sometime for cert renewal
        while time.time() - start < timeout:
            cmd = self.master.run_command(['getcert', 'list'])
            count = cmd.stdout_text.count('status: MONITORING')
            if count == cert_count:
                break
            time.sleep(100)
        else:
            # timeout
            raise AssertionError('TimeOut: Failed to renew all the certs')

        # move date after 3 days of actual expiry
        cert_expiry = cert_expiry + timedelta(days=3)
        cert_expiry = datetime.strftime(cert_expiry, "%Y-%m-%d %H:%M:%S")
        self.master.run_command(['date', '-s', cert_expiry])

        passwd = "{passwd}\n{passwd}\n{passwd}".format(passwd=admin_pass)
        self.master.run_command(['kinit', 'admin'], stdin_text=passwd)
        cmd = self.master.run_command(['ipa-kra-install', '-p', dm_pass, '-U'])
        self.master.run_command(['systemctl', 'start', 'chronyd'])


class TestMaskInstall(IntegrationTest):
    """ Test master and replica installation with wrong mask

    This test checks that master/replica installation fails (expectedly) if
    mask > 022.

    related ticket: https://pagure.io/freeipa/issue/7193
    """

    num_replicas = 0

    @classmethod
    def install(cls, mh):
        super(TestMaskInstall, cls).install(mh)
        cls.bashrc_file = cls.master.get_file_contents('/root/.bashrc')

    def test_install_master(self):
        self.master.run_command('echo "umask 0027" >> /root/.bashrc')
        result = self.master.run_command(['umask'])
        assert '0027' in result.stdout_text

        cmd = tasks.install_master(
            self.master, setup_dns=False, raiseonerr=False
        )
        exp_str = ("Unexpected system mask")
        assert (exp_str in cmd.stderr_text and cmd.returncode != 0)

    def test_install_replica(self):
        result = self.master.run_command(['umask'])
        assert '0027' in result.stdout_text

        cmd = self.master.run_command([
            'ipa-replica-install', '-w', self.master.config.admin_password,
            '-n', self.master.domain.name, '-r', self.master.domain.realm,
            '--server', 'dummy_master.%s' % self.master.domain.name,
            '-U'], raiseonerr=False
        )
        exp_str = ("Unexpected system mask")
        assert (exp_str in cmd.stderr_text and cmd.returncode != 0)

    def test_files_ownership_and_permission_teardown(self):
        """ Method to restore the default bashrc contents"""
        if self.bashrc_file is not None:
            self.master.put_file_contents('/root/.bashrc', self.bashrc_file)


class TestInstallMasterReplica(IntegrationTest):
    """https://pagure.io/freeipa/issue/7929
    Problem:
    If a replica installation fails before all the services
    have been enabled then
    it could leave things in a bad state.

    ipa-replica-manage del --cleanup --force
    invalid 'PKINIT enabled server': all masters must have
    IPA master role enabled

    Root cause was that configuredServices were being
    considered when determining what masters provide
    what services, so a partially installed master
    could cause operations to fail on other masters,
    to the point where a broken master couldn't be removed.
    """
    num_replicas = 1
    topology = 'star'

    @classmethod
    def install(cls, mh):
        tasks.install_master(cls.master, setup_kra=True)
        # do not install KRA on replica, it is part of test
        tasks.install_replica(cls.master, cls.replicas[0], setup_kra=False)

    def test_replicamanage_del(self):
        """Test Steps:
        1. Setup server
        2. Setup replica
        3. modify the replica entry on Master:
           ldapmodify -D cn="Directory Manager"-w <passwd>
           dn: cn=KDC,cn=<replicaFQDN>,cn=masters,cn=ipa,cn=etc,<baseDN>
           changetype: modify
           delete: ipaconfigstring
           ipaconfigstring: enabledService

           dn: cn=KDC,cn=<replicaFQDN>,cn=masters,cn=ipa,cn=etc,<baseDN>
           add: ipaconfigstring
           ipaconfigstring: configuredService
        4. On master,
           run ipa-replica-manage del <replicaFQDN> --cleanup --force
        """
        # https://pagure.io/freeipa/issue/7929
        # modify the replica entry on Master
        cmd_output = None
        dn_entry = 'dn: cn=KDC,cn=%s,cn=masters,cn=ipa,' \
                   'cn=etc,%s' % \
                   (self.replicas[0].hostname,
                    ipautil.realm_to_suffix(
                        self.replicas[0].domain.realm).ldap_text())
        entry_ldif = textwrap.dedent("""
            {dn}
            changetype: modify
            delete: ipaconfigstring
            ipaconfigstring: enabledService

            {dn}
            add: ipaconfigstring
            ipaconfigstring: configuredService
        """).format(dn=dn_entry)
        cmd_output = tasks.ldapmodify_dm(self.master, entry_ldif)
        assert 'modifying entry' in cmd_output.stdout_text

        cmd_output = self.master.run_command([
            'ipa-replica-manage', 'del',
            self.replicas[0].hostname, '--cleanup', '--force'
        ])

        assert_text = 'Deleted IPA server "%s"' % self.replicas[0].hostname
        assert assert_text in cmd_output.stdout_text


class TestInstallReplicaAgainstSpecificServer(IntegrationTest):
    """Installation of replica against a specific server

    Test to check replica install against specific server. It uses master and
    replica1 without CA and having custodia service stopped. Then try to
    install replica2 from replica1 and expect it to get fail as specified
    server is not providing all the services.

    related ticket: https://pagure.io/freeipa/issue/7566
    """

    num_replicas = 2

    @classmethod
    def install(cls, mh):
        tasks.install_master(cls.master, setup_kra=True)

        # install replica1 without CA
        cmd = tasks.install_replica(cls.master, cls.replicas[0],
                                    setup_ca=False, setup_dns=True,
                                    promote=False)

        # check for warning that CA is not installed on server
        warn = 'WARNING: The CA service is only installed on one server'
        assert warn in cmd.stderr_text

    def test_replica_install_against_server_without_ca(self):
        """Replica install will fail complaining about CA role
        and exit code 4"""

        # stop custodia service on replica1
        self.replicas[0].run_command('systemctl stop ipa-custodia.service')

        # check if custodia service is stopped
        cmd = self.replicas[0].run_command('ipactl status')
        assert 'ipa-custodia Service: STOPPED' in cmd.stdout_text

        try:
            # install replica2 against replica1, as CA is not installed on
            # replica1, installation on replica2 should fail
            cmd = tasks.install_replica(self.replicas[0], self.replicas[1],
                                        promote=False, raiseonerr=False)
            assert cmd.returncode == 4
            error = "please provide a server with the CA role"
            assert error in cmd.stderr_text

        finally:
            tasks.uninstall_master(self.replicas[1],
                                   ignore_topology_disconnect=True,
                                   ignore_last_of_role=True)

    def test_replica_install_against_server_without_kra(self):
        """Replica install will fail complaining about KRA role
        and exit code 4"""

        # install ca on replica1
        tasks.install_ca(self.replicas[0])
        try:
            # install replica2 against replica1, as KRA is not installed on
            # replica1(CA installed), installation should fail on replica2
            cmd = tasks.install_replica(self.replicas[0], self.replicas[1],
                                        promote=False, setup_kra=True,
                                        raiseonerr=False)
            assert cmd.returncode == 4
            error = "please provide a server with the KRA role"
            assert error in cmd.stderr_text

        finally:
            tasks.uninstall_master(self.replicas[1],
                                   ignore_topology_disconnect=True,
                                   ignore_last_of_role=True)

    def test_replica_install_against_server(self):
        """Replica install should succeed if specified server provide all
        the services"""

        tasks.install_replica(self.master, self.replicas[1],
                              setup_dns=True, promote=False)

        # check if replication agreement stablished between master
        # and replica2 only.
        cmd = self.replicas[1].run_command(['ipa-replica-manage', 'list',
                                            self.replicas[0].hostname])
        assert self.replicas[0].hostname not in cmd.stdout_text

        dirman_password = self.master.config.dirman_password
        cmd = self.replicas[1].run_command(['ipa-csreplica-manage', 'list',
                                            self.replicas[0].hostname],
                                           stdin_text=dirman_password)
        assert self.replicas[0].hostname not in cmd.stdout_text
