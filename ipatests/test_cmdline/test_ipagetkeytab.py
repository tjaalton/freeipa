# Authors:
#   Rob Crittenden <rcritten@redhat.com>
#
# Copyright (C) 2010  Red Hat
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
"""
Test `ipa-getkeytab`
"""

from __future__ import absolute_import

import os
import shutil
import tempfile

import gssapi
import pytest

from ipalib import api
from ipaplatform.paths import paths
from ipapython import ipautil, ipaldap
from ipaserver.plugins.ldap2 import ldap2
from ipatests.test_cmdline.cmdline import cmdline_test
from ipatests.test_xmlrpc.tracker import host_plugin, service_plugin

def use_keytab(principal, keytab):
    try:
        tmpdir = tempfile.mkdtemp(prefix = "tmp-")
        ccache_file = 'FILE:%s/ccache' % tmpdir
        name = gssapi.Name(principal, gssapi.NameType.kerberos_principal)
        store = {'ccache': ccache_file,
                 'client_keytab': keytab}
        os.environ['KRB5CCNAME'] = ccache_file
        gssapi.Credentials(name=name, usage='initiate', store=store)
        conn = ldap2(api)
        conn.connect(autobind=ipaldap.AUTOBIND_DISABLED)
        conn.disconnect()
    except gssapi.exceptions.GSSError as e:
        raise Exception('Unable to bind to LDAP. Error initializing principal %s in %s: %s' % (principal, keytab, str(e)))
    finally:
        os.environ.pop('KRB5CCNAME', None)
        if tmpdir:
            shutil.rmtree(tmpdir)


@pytest.fixture(scope='class')
def test_host(request):
    host_tracker = host_plugin.HostTracker(u'test-host')
    return host_tracker.make_fixture(request)


@pytest.fixture(scope='class')
def test_service(request, test_host):
    service_tracker = service_plugin.ServiceTracker(u'srv', test_host.name)
    test_host.ensure_exists()
    return service_tracker.make_fixture(request)


@pytest.mark.needs_ipaapi
class KeytabRetrievalTest(cmdline_test):
    """
    Base class for keytab retrieval tests
    """
    command = "ipa-getkeytab"
    keytabname = None

    @classmethod
    def setup_class(cls):
        super(KeytabRetrievalTest, cls).setup_class()

        keytabfd, keytabname = tempfile.mkstemp()

        os.close(keytabfd)
        os.unlink(keytabname)

        cls.keytabname = keytabname

    @classmethod
    def teardown_class(cls):
        super(KeytabRetrievalTest, cls).teardown_class()

        try:
            os.unlink(cls.keytabname)
        except OSError:
            pass

    def run_ipagetkeytab(self, service_principal, args=tuple(),
                         raiseonerr=False):
        new_args = [self.command,
                    "-p", service_principal,
                    "-k", self.keytabname]

        if not args:
            new_args.extend(['-s', api.env.host])
        else:
            new_args.extend(list(args))

        return ipautil.run(
            new_args,
            stdin=None,
            raiseonerr=raiseonerr,
            capture_error=True)

    def assert_success(self, *args, **kwargs):
        result = self.run_ipagetkeytab(*args, **kwargs)
        expected = 'Keytab successfully retrieved and stored in: %s\n' % (
            self.keytabname)
        assert expected in result.error_output, (
            'Success message not in output:\n%s' % result.error_output)

    def assert_failure(self, retcode, message, *args, **kwargs):
        result = self.run_ipagetkeytab(*args, **kwargs)
        err = result.error_output

        assert message in err
        rc = result.returncode
        assert rc == retcode


@pytest.mark.tier0
class test_ipagetkeytab(KeytabRetrievalTest):
    """
    Test `ipa-getkeytab`.
    """
    command = "ipa-getkeytab"
    keytabname = None

    def test_1_run(self, test_service):
        """
        Create a keytab with `ipa-getkeytab` for a non-existent service.
        """
        test_service.ensure_missing()
        result = self.run_ipagetkeytab(test_service.name)
        err = result.error_output

        assert 'Failed to parse result: PrincipalName not found.\n' in err, err
        rc = result.returncode
        assert rc > 0, rc

    def test_2_run(self, test_service):
        """
        Create a keytab with `ipa-getkeytab` for an existing service.
        """
        test_service.ensure_exists()

        self.assert_success(test_service.name, raiseonerr=True)

    def test_3_use(self, test_service):
        """
        Try to use the service keytab.
        """
        use_keytab(test_service.name, self.keytabname)

    def test_4_disable(self, test_service):
        """
        Disable a kerberos principal
        """
        retrieve_cmd = test_service.make_retrieve_command()
        result = retrieve_cmd()
        # Verify that it has a principal key
        assert result[u'result'][u'has_keytab']

        # Disable it
        disable_cmd = test_service.make_disable_command()
        disable_cmd()

        # Verify that it looks disabled
        result = retrieve_cmd()
        assert not result[u'result'][u'has_keytab']

    def test_5_use_disabled(self, test_service):
        """
        Try to use the disabled keytab
        """
        try:
            use_keytab(test_service.name, self.keytabname)
        except Exception as errmsg:
            assert('Unable to bind to LDAP. Error initializing principal' in str(errmsg))

    def test_dangling_symlink(self, test_service):
        # see https://pagure.io/freeipa/issue/4607
        test_service.ensure_exists()

        fd, symlink_target = tempfile.mkstemp()
        os.close(fd)
        os.unlink(symlink_target)
        # create dangling symlink
        os.symlink(self.keytabname, symlink_target)

        try:
            self.assert_success(test_service.name, raiseonerr=True)
            assert os.path.isfile(symlink_target)
            assert os.path.samefile(self.keytabname, symlink_target)
        finally:
            os.unlink(symlink_target)


class TestBindMethods(KeytabRetrievalTest):
    """
    Class that tests '-c'/'-H'/'-Y' flags
    """

    dm_password = None
    ca_cert = None

    @classmethod
    def setup_class(cls):
        super(TestBindMethods, cls).setup_class()

        dmpw_file = os.path.join(api.env.dot_ipa, '.dmpw')

        if not os.path.isfile(dmpw_file):
            pytest.skip('{} file required for this test'.format(dmpw_file))

        with open(dmpw_file, 'r') as f:
            cls.dm_password = f.read().strip()

        tempfd, temp_ca_cert = tempfile.mkstemp()

        os.close(tempfd)

        shutil.copy(os.path.join(paths.IPA_CA_CRT), temp_ca_cert)

        cls.ca_cert = temp_ca_cert

    @classmethod
    def teardown_class(cls):
        super(TestBindMethods, cls).teardown_class()

        try:
            os.unlink(cls.ca_cert)
        except OSError:
            pass

    def check_ldapi(self):
        if not api.env.ldap_uri.startswith('ldapi://'):
            pytest.skip("LDAP URI not pointing to LDAPI socket")

    def test_retrieval_with_dm_creds(self, test_service):
        test_service.ensure_exists()

        self.assert_success(
            test_service.name,
            args=[
                '-D', "cn=Directory Manager",
                '-w', self.dm_password,
                '-s', api.env.host])

    def test_retrieval_using_plain_ldap(self, test_service):
        test_service.ensure_exists()
        ldap_uri = 'ldap://{}'.format(api.env.host)

        self.assert_success(
            test_service.name,
            args=[
                '-D', "cn=Directory Manager",
                '-w', self.dm_password,
                '-H', ldap_uri])

    @pytest.mark.skipif(os.geteuid() != 0,
                        reason="Must have root privileges to run this test")
    def test_retrieval_using_ldapi_external(self, test_service):
        test_service.ensure_exists()
        self.check_ldapi()

        self.assert_success(
            test_service.name,
            args=[
                '-Y',
                'EXTERNAL',
                '-H', api.env.ldap_uri])

    def test_retrieval_using_ldap_gssapi(self, test_service):
        test_service.ensure_exists()
        self.check_ldapi()

        self.assert_success(
            test_service.name,
            args=[
                '-Y',
                'GSSAPI',
                '-H', api.env.ldap_uri])

    def test_retrieval_using_ldaps_ca_cert(self, test_service):
        test_service.ensure_exists()

        self.assert_success(
            test_service.name,
            args=[
                '-D', "cn=Directory Manager",
                '-w', self.dm_password,
                '-H', 'ldaps://{}'.format(api.env.host),
                '--cacert', self.ca_cert])

    def test_ldap_uri_server_raises_error(self, test_service):
        test_service.ensure_exists()

        self.assert_failure(
            2,
            "Cannot specify server and LDAP uri simultaneously",
            test_service.name,
            args=[
                '-H', 'ldaps://{}'.format(api.env.host),
                '-s', api.env.host],
            raiseonerr=False)

    def test_invalid_mech_raises_error(self, test_service):
        test_service.ensure_exists()

        self.assert_failure(
            2,
            "Invalid SASL bind mechanism",
            test_service.name,
            args=[
                '-H', 'ldaps://{}'.format(api.env.host),
                '-Y', 'BOGUS'],
            raiseonerr=False)

    def test_mech_bind_dn_raises_error(self, test_service):
        test_service.ensure_exists()

        self.assert_failure(
            2,
            "Cannot specify both SASL mechanism and bind DN simultaneously",
            test_service.name,
            args=[
                '-D', "cn=Directory Manager",
                '-w', self.dm_password,
                '-H', 'ldaps://{}'.format(api.env.host),
                '-Y', 'EXTERNAL'],
            raiseonerr=False)
