#! /usr/bin/python2 -E
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

from __future__ import print_function

import sys
import tempfile
from optparse import SUPPRESS_HELP

from textwrap import dedent
from ipalib import api
from ipalib.constants import DOMAIN_LEVEL_0
from ipaplatform.paths import paths
from ipapython import admintool
from ipapython import ipautil
from ipaserver.install import service
from ipaserver.install import cainstance
from ipaserver.install import krainstance
from ipaserver.install import dsinstance
from ipaserver.install import installutils
from ipaserver.install.installutils import create_replica_config
from ipaserver.install import dogtaginstance
from ipaserver.install import kra
from ipaserver.install.installutils import ReplicaConfig


class KRAInstall(admintool.AdminTool):

    command_name = 'ipa-kra-install'

    usage = "%prog [options] [replica_file]"

    description = "Install a master or replica KRA."

    @classmethod
    def add_options(cls, parser, debug_option=True):
        super(KRAInstall, cls).add_options(parser, debug_option=True)

        parser.add_option(
            "--no-host-dns", dest="no_host_dns", action="store_true",
            default=False,
            help="Do not use DNS for hostname lookup during installation")

        parser.add_option(
            "-p", "--password",
            dest="password", sensitive=True,
            help="Directory Manager (existing master) password")

        parser.add_option(
            "-U", "--unattended",
            dest="unattended", action="store_true", default=False,
            help="unattended installation never prompts the user")

        parser.add_option(
            "--uninstall",
            dest="uninstall", action="store_true", default=False,
            help=SUPPRESS_HELP)

    def validate_options(self, needs_root=True):
        super(KRAInstall, self).validate_options(needs_root=True)

        installutils.check_server_configuration()

        api.bootstrap(in_server=True, confdir=paths.ETC_IPA)
        api.finalize()

    @classmethod
    def get_command_class(cls, options, args):
        if options.uninstall:
            sys.exit(
                'ERROR: Standalone KRA uninstallation was removed in '
                'FreeIPA 4.5 as it had never worked properly and only caused '
                'issues.')
        else:
            return KRAInstaller


class KRAInstaller(KRAInstall):
    log_file_name = paths.IPASERVER_KRA_INSTALL_LOG

    INSTALLER_START_MESSAGE = '''
        ===================================================================
        This program will setup Dogtag KRA for the FreeIPA Server.

    '''

    FAIL_MESSAGE = '''
        Your system may be partly configured.
        If you run into issues, you may have to re-install IPA on this server.
    '''

    def validate_options(self, needs_root=True):
        super(KRAInstaller, self).validate_options(needs_root=True)

        if self.options.unattended and self.options.password is None:
            self.option_parser.error(
                "Directory Manager password must be specified using -p"
                " in unattended mode"
            )

        if len(self.args) > 1:
            self.option_parser.error("Too many arguments provided")
        elif len(self.args) == 1:
            self.replica_file = self.args[0]
            if not ipautil.file_exists(self.replica_file):
                self.option_parser.error(
                    "Replica file %s does not exist" % self.replica_file)

    def ask_for_options(self):
        super(KRAInstaller, self).ask_for_options()

        if not self.options.unattended and self.options.password is None:
            self.options.password = installutils.read_password(
                "Directory Manager", confirm=False,
                validate=False, retry=False)
            if self.options.password is None:
                raise admintool.ScriptError(
                    "Directory Manager password required")

    def run(self):
        super(KRAInstaller, self).run()

        # Verify DM password. This has to be called after ask_for_options(),
        # so it can't be placed in validate_options().
        try:
            installutils.validate_dm_password_ldap(self.options.password)
        except ValueError:
            raise admintool.ScriptError(
                "Directory Manager password is invalid")

        if not cainstance.is_ca_installed_locally():
            raise RuntimeError("Dogtag CA is not installed. "
                               "Please install the CA first")

        # check if KRA is not already installed
        _kra = krainstance.KRAInstance(api)
        if _kra.is_installed():
            raise admintool.ScriptError("KRA already installed")

        # this check can be done only when CA is installed
        self.installing_replica = dogtaginstance.is_installing_replica("KRA")
        self.options.promote = False

        if self.installing_replica:
            domain_level = dsinstance.get_domain_level(api)
            if domain_level > DOMAIN_LEVEL_0:
                self.options.promote = True
            elif not self.args:
                raise RuntimeError("A replica file is required.")

        if self.args and (not self.installing_replica or self.options.promote):
            raise RuntimeError("Too many parameters provided. "
                               "No replica file is required.")

        self.options.dm_password = self.options.password
        self.options.setup_ca = False
        self.options.setup_kra = True

        api.Backend.ldap2.connect()

        config = None
        if self.installing_replica:
            if self.options.promote:
                config = ReplicaConfig()
                config.kra_host_name = None
                config.realm_name = api.env.realm
                config.host_name = api.env.host
                config.domain_name = api.env.domain
                config.dirman_password = self.options.password
                config.ca_ds_port = 389
                config.top_dir = tempfile.mkdtemp("ipa")
                config.dir = config.top_dir
            else:
                config = create_replica_config(
                    self.options.password,
                    self.replica_file,
                    self.options)
                config.kra_host_name = config.master_host_name

            config.setup_kra = True

            if config.subject_base is None:
                attrs = api.Backend.ldap2.get_ipa_config()
                config.subject_base = attrs.get('ipacertificatesubjectbase')[0]

            if config.kra_host_name is None:
                config.kra_host_name = service.find_providing_server(
                    'KRA', api.Backend.ldap2, api.env.ca_host)

        try:
            kra.install_check(api, config, self.options)
        except RuntimeError as e:
            raise admintool.ScriptError(str(e))

        print(dedent(self.INSTALLER_START_MESSAGE))

        try:
            kra.install(api, config, self.options)
        except:
            self.log.error(dedent(self.FAIL_MESSAGE))
            raise

        api.Backend.ldap2.disconnect()
