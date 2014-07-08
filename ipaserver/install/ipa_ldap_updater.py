# Authors: Rob Crittenden <rcritten@redhat.com>
#          Petr Viktorin <pviktori@redhat.com>
#
# Copyright (C) 2008  Red Hat
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

# Documentation can be found at http://freeipa.org/page/LdapUpdate

# TODO
# save undo files?

import os
import sys

import krbV

from ipalib import api
from ipapython import ipautil, admintool
from ipaplatform.paths import paths
from ipaserver.install import installutils, dsinstance, schemaupdate
from ipaserver.install.ldapupdate import LDAPUpdate, UPDATES_DIR
from ipaserver.install.upgradeinstance import IPAUpgrade


class LDAPUpdater(admintool.AdminTool):
    command_name = 'ipa-ldap-updater'

    usage = "%prog [options] input_file(s)\n"
    usage += "%prog [options]\n"

    @classmethod
    def add_options(cls, parser):
        super(LDAPUpdater, cls).add_options(parser, debug_option=True)

        parser.add_option("-t", "--test", action="store_true", dest="test",
            default=False,
            help="run through the update without changing anything")
        parser.add_option("-y", dest="password",
            help="file containing the Directory Manager password")
        parser.add_option("-l", '--ldapi', action="store_true", dest="ldapi",
            default=False,
            help="connect to the LDAP server using the ldapi socket")
        parser.add_option("-u", '--upgrade', action="store_true",
            dest="upgrade", default=False,
            help="upgrade an installed server in offline mode")
        parser.add_option("-p", '--plugins', action="store_true",
            dest="plugins", default=False,
            help="execute update plugins " +
                "(implied when no input files are given)")
        parser.add_option("-s", '--schema', action="store_true",
            dest="update_schema", default=False,
            help="update the schema "
                "(implied when no input files are given)")
        parser.add_option("-S", '--schema-file', action="append",
            dest="schema_files",
            help="custom schema ldif file to use (implies -s)")
        parser.add_option("-W", '--password', action="store_true",
            dest="ask_password",
            help="prompt for the Directory Manager password")

    @classmethod
    def get_command_class(cls, options, args):
        if options.upgrade:
            return LDAPUpdater_Upgrade
        else:
            return LDAPUpdater_NonUpgrade

    def validate_options(self, **kwargs):
        options = self.options
        super(LDAPUpdater, self).validate_options(**kwargs)

        self.files = self.args

        for filename in self.files:
            if not os.path.exists(filename):
                raise admintool.ScriptError("%s: file not found" % filename)

        if os.getegid() == 0:
            try:
                installutils.check_server_configuration()
            except RuntimeError, e:
                print unicode(e)
                sys.exit(1)
        elif not os.path.exists(paths.IPA_DEFAULT_CONF):
            print "IPA is not configured on this system."
            sys.exit(1)

        if options.password:
            pw = ipautil.template_file(options.password, [])
            self.dirman_password = pw.strip()
        else:
            self.dirman_password = None

        if options.schema_files or not self.files:
            options.update_schema = True
        if not options.schema_files:
            options.schema_files = [os.path.join(ipautil.SHARE_DIR, f) for f
                                    in dsinstance.ALL_SCHEMA_FILES]

    def setup_logging(self):
        super(LDAPUpdater, self).setup_logging(log_file_mode='a')

    def run(self):
        super(LDAPUpdater, self).run()

        api.bootstrap(in_server=True, context='updates')
        api.finalize()

    def handle_error(self, exception):
        return installutils.handle_error(exception, self.log_file_name)


class LDAPUpdater_Upgrade(LDAPUpdater):
    log_file_name = paths.IPAUPGRADE_LOG

    def validate_options(self):
        if os.getegid() != 0:
            raise admintool.ScriptError('Must be root to do an upgrade.', 1)

        super(LDAPUpdater_Upgrade, self).validate_options(needs_root=True)

    def run(self):
        super(LDAPUpdater_Upgrade, self).run()
        options = self.options

        updates = None
        realm = krbV.default_context().default_realm
        upgrade = IPAUpgrade(realm, self.files, live_run=not options.test,
                             schema_files=options.schema_files)
        upgrade.create_instance()
        upgradefailed = upgrade.upgradefailed

        if upgrade.badsyntax:
            raise admintool.ScriptError(
                'Bad syntax detected in upgrade file(s).', 1)
        elif upgrade.upgradefailed:
            raise admintool.ScriptError('IPA upgrade failed.', 1)
        elif upgrade.modified and options.test:
            self.log.info('Update complete, changes to be made, test mode')
            return 2


class LDAPUpdater_NonUpgrade(LDAPUpdater):
    log_file_name = paths.IPAUPGRADE_LOG

    def validate_options(self):
        super(LDAPUpdater_NonUpgrade, self).validate_options()
        options = self.options

        # Only run plugins if no files are given
        self.run_plugins = not self.files or options.plugins

        # Need root for running plugins
        if os.getegid() != 0:
            if self.run_plugins:
                raise admintool.ScriptError(
                    'Plugins can only be run as root.', 1)
            else:
                # Can't log to the default file as non-root
                self.log_file_name = None

    def ask_for_options(self):
        super(LDAPUpdater_NonUpgrade, self).ask_for_options()
        options = self.options
        if not self.dirman_password:
            if options.ask_password or not options.ldapi:
                password = installutils.read_password("Directory Manager",
                    confirm=False, validate=False)
                if password is None:
                    raise admintool.ScriptError(
                        "Directory Manager password required")
                self.dirman_password = password

    def run(self):
        super(LDAPUpdater_NonUpgrade, self).run()
        options = self.options

        modified = False

        ld = LDAPUpdate(
            dm_password=self.dirman_password,
            sub_dict={},
            live_run=not options.test,
            ldapi=options.ldapi,
            plugins=options.plugins or self.run_plugins)

        modified = ld.pre_schema_update(ordered=True)

        if options.update_schema:
            modified = schemaupdate.update_schema(
                options.schema_files,
                dm_password=self.dirman_password,
                live_run=not options.test,
                ldapi=options.ldapi) or modified

        if not self.files:
            self.files = ld.get_all_files(UPDATES_DIR)

        modified = ld.update(self.files, ordered=True) or modified

        if modified and options.test:
            self.log.info('Update complete, changes to be made, test mode')
            return 2
