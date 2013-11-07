# Author: Alexander Bokovoy <abokovoy@redhat.com>
#
# Copyright (C) 2011   Red Hat
# see file 'COPYING' for use and warranty information
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.    See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

import os
import time

from ipapython import ipautil, dogtag
from ipapython.platform import base
from ipapython.platform.base import systemd
from ipapython.platform.fedora16 import selinux
from ipapython.ipa_log_manager import root_logger
from ipalib import api

# For beginning just remap names to add .service
# As more services will migrate to systemd, unit names will deviate and
# mapping will be kept in this dictionary
system_units = dict(map(lambda x: (x, "%s.service" % (x)), base.wellknownservices))

system_units['rpcgssd'] = 'nfs-secure.service'
system_units['rpcidmapd'] = 'nfs-idmap.service'

# Rewrite dirsrv and pki-tomcatd services as they support instances via separate
# service generator. To make this working, one needs to have both foo@.servic
# and foo.target -- the latter is used when request should be coming for
# all instances (like stop). systemd, unfortunately, does not allow one
# to request action for all service instances at once if only foo@.service
# unit is available. To add more, if any of those services need to be
# started/stopped automagically, one needs to manually create symlinks in
# /etc/systemd/system/foo.target.wants/ (look into systemd.py's enable()
# code).
system_units['dirsrv'] = 'dirsrv@.service'
# Our directory server instance for PKI is dirsrv@PKI-IPA.service
system_units['pkids'] = 'dirsrv@PKI-IPA.service'
# Old style PKI instance
system_units['pki-cad'] = 'pki-cad@pki-ca.service'
system_units['pki_cad'] = system_units['pki-cad']
# Our PKI instance is pki-tomcatd@pki-tomcat.service
system_units['pki-tomcatd'] = 'pki-tomcatd@pki-tomcat.service'
system_units['pki_tomcatd'] = system_units['pki-tomcatd']
system_units['ipa-otpd'] = 'ipa-otpd.socket'

class Fedora16Service(systemd.SystemdService):
    def __init__(self, service_name):
        systemd_name = service_name
        if service_name in system_units:
            systemd_name = system_units[service_name]
        else:
            if len(service_name.split('.')) == 1:
                # if service_name does not have a dot, it is not foo.service
                # and not a foo.target. Thus, not correct service name for
                # systemd, default to foo.service style then
                systemd_name = "%s.service" % (service_name)
        super(Fedora16Service, self).__init__(service_name, systemd_name)

# Special handling of directory server service
#
# We need to explicitly enable instances to install proper symlinks as
# dirsrv.target.wants/ dependencies. Standard systemd service class does it
# on enable() method call. Unfortunately, ipa-server-install does not do
# explicit dirsrv.enable() because the service startup is handled by ipactl.
#
# If we wouldn't do this, our instances will not be started as systemd would
# not have any clue about instances (PKI-IPA and the domain we serve) at all.
# Thus, hook into dirsrv.restart().


class Fedora16DirectoryService(Fedora16Service):

    def tune_nofile_platform(self, num=8192, fstore=None):
        """
        Increase the number of files descriptors available to directory server
        from the default 1024 to 8192. This will allow to support a greater
        number of clients out of the box.

        This is a part of the implementation that is systemd-specific.

        Returns False if the setting of the nofile limit needs to be skipped.
        """

        dirsrv_systemd = "/etc/sysconfig/dirsrv.systemd"

        if os.path.exists(dirsrv_systemd):
            # We need to enable LimitNOFILE=8192 in the dirsrv@.service
            # Since 389-ds-base-1.2.10-0.8.a7 the configuration of the
            # service parameters is performed via
            # /etc/sysconfig/dirsrv.systemd file which is imported by systemd
            # into dirsrv@.service unit
            replacevars = {'LimitNOFILE': str(num)}
            ipautil.inifile_replace_variables(dirsrv_systemd,
                                              'service',
                                              replacevars=replacevars)
            selinux.restore_context(dirsrv_systemd)
            ipautil.run(["/bin/systemctl", "--system", "daemon-reload"],
                        raiseonerr=False)

        return True

    def restart(self, instance_name="", capture_output=True, wait=True):
        if len(instance_name) > 0:
            elements = self.systemd_name.split("@")
            srv_etc = os.path.join(self.SYSTEMD_ETC_PATH, self.systemd_name)
            srv_tgt = os.path.join(self.SYSTEMD_ETC_PATH, self.SYSTEMD_SRV_TARGET % (elements[0]))
            srv_lnk = os.path.join(srv_tgt, self.service_instance(instance_name))
            if not os.path.exists(srv_etc):
                self.enable(instance_name)
            elif not os.path.samefile(srv_etc, srv_lnk):
                os.unlink(srv_lnk)
                os.symlink(srv_etc, srv_lnk)
        super(Fedora16DirectoryService, self).restart(instance_name, capture_output=capture_output, wait=wait)

# Enforce restart of IPA services when we do enable it
# This gets around the fact that after ipa-server-install systemd thinks
# ipa.service is not yet started but all services were actually started
# already.
class Fedora16IPAService(Fedora16Service):
    def enable(self, instance_name=""):
        super(Fedora16IPAService, self).enable(instance_name)
        self.restart(instance_name)

class Fedora16SSHService(Fedora16Service):
    def get_config_dir(self, instance_name=""):
        return '/etc/ssh'

class Fedora16CAService(Fedora16Service):
    def __wait_until_running(self):
        # We must not wait for the httpd proxy if httpd is not set up yet.
        # Unfortunately, knownservices.httpd.is_installed() can return
        # false positives, so check for existence of our configuration file.
        # TODO: Use a cleaner solution
        use_proxy = True
        if not (os.path.exists('/etc/httpd/conf.d/ipa.conf') and
                os.path.exists('/etc/httpd/conf.d/ipa-pki-proxy.conf')):
            root_logger.debug(
                'The httpd proxy is not installed, wait on local port')
            use_proxy = False
        root_logger.debug('Waiting until the CA is running')
        timeout = api.env.startup_timeout
        op_timeout = time.time() + timeout
        while time.time() < op_timeout:
            try:
                status = dogtag.ca_status(use_proxy=use_proxy)
            except Exception:
                status = 'check interrupted'
            root_logger.debug('The CA status is: %s' % status)
            if status == 'running':
                break
            root_logger.debug('Waiting for CA to start...')
            time.sleep(1)
        else:
            raise RuntimeError('CA did not start in %ss' % timeout)

    def start(self, instance_name="", capture_output=True, wait=True):
        super(Fedora16CAService, self).start(
            instance_name, capture_output=capture_output, wait=wait)
        if wait:
            self.__wait_until_running()

    def restart(self, instance_name="", capture_output=True, wait=True):
        super(Fedora16CAService, self).restart(
            instance_name, capture_output=capture_output, wait=wait)
        if wait:
            self.__wait_until_running()

# Redirect directory server service through special sub-class due to its
# special handling of instances
def f16_service(name):
    if name == 'dirsrv':
        return Fedora16DirectoryService(name)
    if name == 'ipa':
        return Fedora16IPAService(name)
    if name == 'sshd':
        return Fedora16SSHService(name)
    if name in ('pki-cad', 'pki_cad', 'pki-tomcatd', 'pki_tomcatd'):
        return Fedora16CAService(name)
    return Fedora16Service(name)

class Fedora16Services(base.KnownServices):
    def __init__(self):
        services = dict()
        for s in base.wellknownservices:
            services[s] = f16_service(s)
        # Call base class constructor. This will lock services to read-only
        super(Fedora16Services, self).__init__(services)
