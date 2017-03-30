# Authors: Simo Sorce <ssorce@redhat.com>
#
# Copyright (C) 2007-2016  Red Hat, Inc.
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

from __future__ import print_function

import string
import tempfile
import subprocess
import random
import os
import sys
import copy
import stat
import shutil
import socket
import re
import datetime
import netaddr
import netifaces
import time
import gssapi
import pwd
import grp
from contextlib import contextmanager
import locale
import collections
from subprocess import CalledProcessError

from dns import resolver, reversename
from dns.exception import DNSException

import six
from six.moves import input
from six.moves import urllib

from ipapython.ipa_log_manager import root_logger
from ipapython import config
from ipaplatform.paths import paths
from ipapython.dn import DN

SHARE_DIR = paths.USR_SHARE_IPA_DIR
PLUGINS_SHARE_DIR = paths.IPA_PLUGINS

GEN_PWD_LEN = 22
GEN_TMP_PWD_LEN = 12  # only for OTP password that is manually retyped by user

# Having this in krb_utils would cause circular import
KRB5_KDC_UNREACH = 2529639068 # Cannot contact any KDC for requested realm
KRB5KDC_ERR_SVC_UNAVAILABLE = 2529638941 # A service is not available that is
                                         # required to process the request


def get_domain_name():
    try:
        config.init_config()
        domain_name = config.config.get_domain()
    except Exception:
        return None

    return domain_name


class UnsafeIPAddress(netaddr.IPAddress):
    """Any valid IP address with or without netmask."""

    # Use inet_pton() rather than inet_aton() for IP address parsing. We
    # will use the same function in IPv4/IPv6 conversions + be stricter
    # and don't allow IP addresses such as '1.1.1' in the same time
    netaddr_ip_flags = netaddr.INET_PTON

    def __init__(self, addr):
        if isinstance(addr, UnsafeIPAddress):
            self._net = addr._net
            super(UnsafeIPAddress, self).__init__(addr,
                                                  flags=self.netaddr_ip_flags)
            return

        elif isinstance(addr, netaddr.IPAddress):
            self._net = None  # no information about netmask
            super(UnsafeIPAddress, self).__init__(addr,
                                                  flags=self.netaddr_ip_flags)
            return

        elif isinstance(addr, netaddr.IPNetwork):
            self._net = addr
            super(UnsafeIPAddress, self).__init__(self._net.ip,
                                                  flags=self.netaddr_ip_flags)
            return

        # option of last resort: parse it as string
        self._net = None
        addr = str(addr)
        try:
            try:
                addr = netaddr.IPAddress(addr, flags=self.netaddr_ip_flags)
            except netaddr.AddrFormatError:
                # netaddr.IPAddress doesn't handle zone indices in textual
                # IPv6 addresses. Try removing zone index and parse the
                # address again.
                addr, sep, foo = addr.partition('%')
                if sep != '%':
                    raise
                addr = netaddr.IPAddress(addr, flags=self.netaddr_ip_flags)
                if addr.version != 6:
                    raise
        except ValueError:
            self._net = netaddr.IPNetwork(addr, flags=self.netaddr_ip_flags)
            addr = self._net.ip
        super(UnsafeIPAddress, self).__init__(addr,
                                              flags=self.netaddr_ip_flags)

    def __getstate__(self):
        state = {
            '_net': self._net,
            'super_state': super(UnsafeIPAddress, self).__getstate__(),
        }
        return state

    def __setstate__(self, state):
        super(UnsafeIPAddress, self).__setstate__(state['super_state'])
        self._net = state['_net']


class CheckedIPAddress(UnsafeIPAddress):
    """IPv4 or IPv6 address with additional constraints.

    Reserved or link-local addresses are never accepted.
    """
    def __init__(self, addr, match_local=False, parse_netmask=True,
                 allow_loopback=False, allow_multicast=False):

        super(CheckedIPAddress, self).__init__(addr)
        if isinstance(addr, CheckedIPAddress):
            self.prefixlen = addr.prefixlen
            return

        if not parse_netmask and self._net:
            raise ValueError(
                "netmask and prefix length not allowed here: {}".format(addr))

        if self.version not in (4, 6):
            raise ValueError("unsupported IP version {}".format(self.version))

        if not allow_loopback and self.is_loopback():
            raise ValueError("cannot use loopback IP address {}".format(addr))
        if (not self.is_loopback() and self.is_reserved()) \
                or self in netaddr.ip.IPV4_6TO4:
            raise ValueError(
                "cannot use IANA reserved IP address {}".format(addr))

        if self.is_link_local():
            raise ValueError(
                "cannot use link-local IP address {}".format(addr))
        if not allow_multicast and self.is_multicast():
            raise ValueError("cannot use multicast IP address {}".format(addr))

        if match_local:
            if self.version == 4:
                family = netifaces.AF_INET
            elif self.version == 6:
                family = netifaces.AF_INET6
            else:
                raise ValueError(
                    "Unsupported address family ({})".format(self.version)
                )

            iface = None
            for interface in netifaces.interfaces():
                for ifdata in netifaces.ifaddresses(interface).get(family, []):

                    # link-local addresses contain '%suffix' that causes parse
                    # errors in IPNetwork
                    ifaddr = ifdata['addr'].split(u'%', 1)[0]

                    ifnet = netaddr.IPNetwork('{addr}/{netmask}'.format(
                        addr=ifaddr,
                        netmask=ifdata['netmask']
                    ))
                    if ifnet == self._net or (
                            self._net is None and ifnet.ip == self):
                        self._net = ifnet
                        iface = interface
                        break

            if iface is None:
                raise ValueError('no network interface matches the IP address '
                                 'and netmask {}'.format(addr))

        if self._net is None:
            if self.version == 4:
                self._net = netaddr.IPNetwork(
                    netaddr.cidr_abbrev_to_verbose(str(self)))
            elif self.version == 6:
                self._net = netaddr.IPNetwork(str(self) + '/64')

        self.prefixlen = self._net.prefixlen

    def __getstate__(self):
        state = {
            'prefixlen': self.prefixlen,
            'super_state': super(CheckedIPAddress, self).__getstate__(),
        }
        return state

    def __setstate__(self, state):
        super(CheckedIPAddress, self).__setstate__(state['super_state'])
        self.prefixlen = state['prefixlen']

    def is_network_addr(self):
        return self == self._net.network

    def is_broadcast_addr(self):
        return self.version == 4 and self == self._net.broadcast


def valid_ip(addr):
    return netaddr.valid_ipv4(addr) or netaddr.valid_ipv6(addr)

def format_netloc(host, port=None):
    """
    Format network location (host:port).

    If the host part is a literal IPv6 address, it must be enclosed in square
    brackets (RFC 2732).
    """
    host = str(host)
    try:
        socket.inet_pton(socket.AF_INET6, host)
        host = '[%s]' % host
    except socket.error:
        pass
    if port is None:
        return host
    else:
        return '%s:%s' % (host, str(port))

def realm_to_suffix(realm_name):
    'Convert a kerberos realm to a IPA suffix.'
    s = realm_name.split(".")
    suffix_dn = DN(*[('dc', x.lower()) for x in s])
    return suffix_dn

def suffix_to_realm(suffix_dn):
    'Convert a IPA suffix to a kerberos realm.'
    assert isinstance(suffix_dn, DN)
    realm = '.'.join([x.value for x in suffix_dn])
    return realm

def template_str(txt, vars):
    val = string.Template(txt).substitute(vars)

    # eval() is a special string one can insert into a template to have the
    # Python interpreter evaluate the string. This is intended to allow
    # math to be performed in templates.
    pattern = re.compile('(eval\s*\(([^()]*)\))')
    val = pattern.sub(lambda x: str(eval(x.group(2))), val)

    return val

def template_file(infilename, vars):
    """Read a file and perform template substitutions"""
    with open(infilename) as f:
        return template_str(f.read(), vars)

def copy_template_file(infilename, outfilename, vars):
    """Copy a file, performing template substitutions"""
    txt = template_file(infilename, vars)
    with open(outfilename, 'w') as file:
        file.write(txt)


def write_tmp_file(txt):
    fd = tempfile.NamedTemporaryFile('w+')
    fd.write(txt)
    fd.flush()

    return fd

def shell_quote(string):
    if isinstance(string, str):
        return "'" + string.replace("'", "'\\''") + "'"
    else:
        return b"'" + string.replace(b"'", b"'\\''") + b"'"


if six.PY3:
    def _log_arg(s):
        """Convert string or bytes to a string suitable for logging"""
        if isinstance(s, bytes):
            return s.decode(locale.getpreferredencoding(),
                            errors='replace')
        else:
            return s
else:
    _log_arg = str


class _RunResult(collections.namedtuple('_RunResult',
                                        'output error_output returncode')):
    """Result of ipautil.run"""


def run(args, stdin=None, raiseonerr=True, nolog=(), env=None,
        capture_output=False, skip_output=False, cwd=None,
        runas=None, timeout=None, suplementary_groups=[],
        capture_error=False, encoding=None, redirect_output=False):
    """
    Execute an external command.

    :param args: List of arguments for the command
    :param stdin: Optional input to the command
    :param raiseonerr: If True, raises an exception if the return code is
        not zero
    :param nolog: Tuple of strings that shouldn't be logged, like passwords.
        Each tuple consists of a string to be replaced by XXXXXXXX.

        Example:
        We have a command
            [paths.SETPASSWD, '--password', 'Secret123', 'someuser']
        and we don't want to log the password so nolog would be set to:
        ('Secret123',)
        The resulting log output would be:

        /usr/bin/setpasswd --password XXXXXXXX someuser

        If a value isn't found in the list it is silently ignored.
    :param env: Dictionary of environment variables passed to the command.
        When None, current environment is copied
    :param capture_output: Capture stdout
    :param skip_output: Redirect the output to /dev/null and do not log it
    :param cwd: Current working directory
    :param runas: Name of a user that the command should be run as. The spawned
        process will have both real and effective UID and GID set.
    :param timeout: Timeout if the command hasn't returned within the specified
        number of seconds.
    :param suplementary_groups: List of group names that will be used as
        suplementary groups for subporcess.
        The option runas must be specified together with this option.
    :param capture_error: Capture stderr
    :param encoding: For Python 3, the encoding to use for output,
        error_output, and (if it's not bytes) stdin.
        If None, the current encoding according to locale is used.
    :param redirect_output: Redirect (error) output to standard (error) output.

    :return: An object with these attributes:

        `returncode`: The process' exit status

        `output` and `error_output`: captured output, as strings. Under
        Python 3, these are encoded with the given `encoding`.
        None unless `capture_output` or `capture_error`, respectively, are
        given

        `raw_output`, `raw_error_output`: captured output, as bytes.

        `output_log` and `error_log`: The captured output, as strings, with any
        unencodable characters discarded. These should only be used
        for logging or error messages.

    If skip_output is given, all output-related attributes on the result
    (that is, all except `returncode`) are None.

    For backwards compatibility, the return value can also be used as a
    (output, error_output, returncode) triple.
    """
    assert isinstance(suplementary_groups, list)
    p_in = None
    p_out = None
    p_err = None

    if isinstance(nolog, six.string_types):
        # We expect a tuple (or list, or other iterable) of nolog strings.
        # Passing just a single string is bad: strings are iterable, so this
        # would result in every individual character of that string being
        # replaced by XXXXXXXX.
        # This is a sanity check to prevent that.
        raise ValueError('nolog must be a tuple of strings.')

    if skip_output and (capture_output or capture_error):
        raise ValueError('skip_output is incompatible with '
                         'capture_output or capture_error')

    if redirect_output and (capture_output or capture_error):
        raise ValueError('redirect_output is incompatible with '
                         'capture_output or capture_error')

    if skip_output and redirect_output:
        raise ValueError('skip_output is incompatible with redirect_output')

    if env is None:
        # copy default env
        env = copy.deepcopy(os.environ)
        env["PATH"] = "/bin:/sbin:/usr/kerberos/bin:/usr/kerberos/sbin:/usr/bin:/usr/sbin"
    if stdin:
        p_in = subprocess.PIPE
    if skip_output:
        p_out = p_err = open(paths.DEV_NULL, 'w')
    elif redirect_output:
        p_out = sys.stdout
        p_err = sys.stderr
    else:
        p_out = subprocess.PIPE
        p_err = subprocess.PIPE

    if encoding is None:
        encoding = locale.getpreferredencoding()

    if six.PY3 and isinstance(stdin, str):
        stdin = stdin.encode(encoding)

    if timeout:
        # If a timeout was provided, use the timeout command
        # to execute the requested command.
        args[0:0] = [paths.BIN_TIMEOUT, str(timeout)]

    arg_string = nolog_replace(' '.join(_log_arg(a) for a in args), nolog)
    root_logger.debug('Starting external process')
    root_logger.debug('args=%s' % arg_string)

    preexec_fn = None
    if runas is not None:
        pent = pwd.getpwnam(runas)

        suplementary_gids = [
            grp.getgrnam(group).gr_gid for group in suplementary_groups
        ]

        root_logger.debug('runas=%s (UID %d, GID %s)', runas,
            pent.pw_uid, pent.pw_gid)
        if suplementary_groups:
            for group, gid in zip(suplementary_groups, suplementary_gids):
                root_logger.debug('suplementary_group=%s (GID %d)', group, gid)

        preexec_fn = lambda: (
            os.setgroups(suplementary_gids),
            os.setregid(pent.pw_gid, pent.pw_gid),
            os.setreuid(pent.pw_uid, pent.pw_uid),
        )

    try:
        p = subprocess.Popen(args, stdin=p_in, stdout=p_out, stderr=p_err,
                             close_fds=True, env=env, cwd=cwd,
                             preexec_fn=preexec_fn)
        stdout, stderr = p.communicate(stdin)
    except KeyboardInterrupt:
        root_logger.debug('Process interrupted')
        p.wait()
        raise
    except:
        root_logger.debug('Process execution failed')
        raise
    finally:
        if skip_output:
            p_out.close()   # pylint: disable=E1103

    if timeout and p.returncode == 124:
        root_logger.debug('Process did not complete before timeout')

    root_logger.debug('Process finished, return code=%s', p.returncode)

    # The command and its output may include passwords that we don't want
    # to log. Replace those.
    if skip_output or redirect_output:
        output_log = None
        error_log = None
    else:
        if six.PY3:
            output_log = stdout.decode(locale.getpreferredencoding(),
                                       errors='replace')
        else:
            output_log = stdout
        if six.PY3:
            error_log = stderr.decode(locale.getpreferredencoding(),
                                      errors='replace')
        else:
            error_log = stderr
        output_log = nolog_replace(output_log, nolog)
        root_logger.debug('stdout=%s' % output_log)
        error_log = nolog_replace(error_log, nolog)
        root_logger.debug('stderr=%s' % error_log)

    if capture_output:
        if six.PY2:
            output = stdout
        else:
            output = stdout.decode(encoding)
    else:
        output = None

    if capture_error:
        if six.PY2:
            error_output = stderr
        else:
            error_output = stderr.decode(encoding)
    else:
        error_output = None

    if p.returncode != 0 and raiseonerr:
        raise CalledProcessError(p.returncode, arg_string, str(output))

    result = _RunResult(output, error_output, p.returncode)
    result.raw_output = stdout
    result.raw_error_output = stderr
    result.output_log = output_log
    result.error_log = error_log
    return result


def nolog_replace(string, nolog):
    """Replace occurences of strings given in `nolog` with XXXXXXXX"""
    for value in nolog:
        if not value or not isinstance(value, six.string_types):
            continue

        quoted = urllib.parse.quote(value)
        shquoted = shell_quote(value)
        for nolog_value in (shquoted, value, quoted):
            string = string.replace(nolog_value, 'XXXXXXXX')
    return string


def file_exists(filename):
    try:
        mode = os.stat(filename)[stat.ST_MODE]
        if stat.S_ISREG(mode):
            return True
        else:
            return False
    except Exception:
        return False

def dir_exists(filename):
    try:
        mode = os.stat(filename)[stat.ST_MODE]
        if stat.S_ISDIR(mode):
            return True
        else:
            return False
    except Exception:
        return False


def install_file(fname, dest):
    # SELinux: use copy to keep the right context
    if file_exists(dest):
        os.rename(dest, dest + ".orig")
    shutil.copy(fname, dest)
    os.remove(fname)


def backup_file(fname):
    if file_exists(fname):
        os.rename(fname, fname + ".orig")

def _ensure_nonempty_string(string, message):
    if not isinstance(string, str) or not string:
        raise ValueError(message)

# uses gpg to compress and encrypt a file
def encrypt_file(source, dest, password, workdir = None):
    _ensure_nonempty_string(source, 'Missing Source File')
    #stat it so that we get back an exception if it does no t exist
    os.stat(source)

    _ensure_nonempty_string(dest, 'Missing Destination File')
    _ensure_nonempty_string(password, 'Missing Password')

    #create a tempdir so that we can clean up with easily
    tempdir = tempfile.mkdtemp('', 'ipa-', workdir)
    gpgdir = tempdir+"/.gnupg"

    try:
        try:
            #give gpg a fake dir so that we can leater remove all
            #the cruft when we clean up the tempdir
            os.mkdir(gpgdir)
            args = [paths.GPG_AGENT, '--batch', '--homedir', gpgdir, '--daemon', paths.GPG, '--batch', '--homedir', gpgdir, '--passphrase-fd', '0', '--yes', '--no-tty', '-o', dest, '-c', source]
            run(args, password, skip_output=True)
        except:
            raise
    finally:
        #job done, clean up
        shutil.rmtree(tempdir, ignore_errors=True)


def decrypt_file(source, dest, password, workdir = None):
    _ensure_nonempty_string(source, 'Missing Source File')
    #stat it so that we get back an exception if it does no t exist
    os.stat(source)

    _ensure_nonempty_string(dest, 'Missing Destination File')
    _ensure_nonempty_string(password, 'Missing Password')

    #create a tempdir so that we can clean up with easily
    tempdir = tempfile.mkdtemp('', 'ipa-', workdir)
    gpgdir = tempdir+"/.gnupg"

    try:
        try:
            #give gpg a fake dir so that we can leater remove all
            #the cruft when we clean up the tempdir
            os.mkdir(gpgdir)
            args = [paths.GPG_AGENT, '--batch', '--homedir', gpgdir, '--daemon', paths.GPG, '--batch', '--homedir', gpgdir, '--passphrase-fd', '0', '--yes', '--no-tty', '-o', dest, '-d', source]
            run(args, password, skip_output=True)
        except:
            raise
    finally:
        #job done, clean up
        shutil.rmtree(tempdir, ignore_errors=True)


class CIDict(dict):
    """
    Case-insensitive but case-respecting dictionary.

    This code is derived from python-ldap's cidict.py module,
    written by stroeder: http://python-ldap.sourceforge.net/

    This version extends 'dict' so it works properly with TurboGears.
    If you extend UserDict, isinstance(foo, dict) returns false.
    """

    def __init__(self, default=None, **kwargs):
        super(CIDict, self).__init__()
        self._keys = {}  # mapping of lowercased keys to proper case
        if default:
            self.update(default)
        if kwargs:
            self.update(kwargs)

    def __getitem__(self, key):
        return super(CIDict, self).__getitem__(key.lower())

    def __setitem__(self, key, value, seen_keys=None):
        """cidict[key] = value

        The ``seen_keys`` argument is used by ``update()`` to keep track of
        duplicate keys. It should be an initially empty set that is
        passed to all calls to __setitem__ that should not set duplicate keys.
        """
        lower_key = key.lower()
        if seen_keys is not None:
            if lower_key in seen_keys:
                raise ValueError('Duplicate key in update: %s' % key)
            seen_keys.add(lower_key)
        self._keys[lower_key] = key
        return super(CIDict, self).__setitem__(lower_key, value)

    def __delitem__(self, key):
        lower_key = key.lower()
        del self._keys[lower_key]
        return super(CIDict, self).__delitem__(lower_key)

    def update(self, new=None, **kwargs):
        """Update self from dict/iterable new and kwargs

        Functions like ``dict.update()``.

        Neither ``new`` nor ``kwargs`` may contain two keys that only differ in
        case, as this situation would result in loss of data.
        """
        seen = set()
        if new:
            try:
                keys = new.keys
            except AttributeError:
                self.update(dict(new))
            else:
                for key in keys():
                    self.__setitem__(key, new[key], seen)
        seen = set()
        for key, value in kwargs.items():
            self.__setitem__(key, value, seen)

    def __contains__(self, key):
        return super(CIDict, self).__contains__(key.lower())

    if six.PY2:
        def has_key(self, key):
            return super(CIDict, self).has_key(key.lower())

    def get(self, key, failobj=None):
        try:
            return self[key]
        except KeyError:
            return failobj

    def __iter__(self):
        return six.itervalues(self._keys)

    def keys(self):
        if six.PY2:
            return list(self.iterkeys())
        else:
            return self.iterkeys()

    def items(self):
        if six.PY2:
            return list(self.iteritems())
        else:
            return self.iteritems()

    def values(self):
        if six.PY2:
            return list(self.itervalues())
        else:
            return self.itervalues()

    def copy(self):
        """Returns a shallow copy of this CIDict"""
        return CIDict(list(self.items()))

    def iteritems(self):
        return ((k, self[k]) for k in six.itervalues(self._keys))

    def iterkeys(self):
        return six.itervalues(self._keys)

    def itervalues(self):
        return (v for k, v in six.iteritems(self))

    def setdefault(self, key, value=None):
        try:
            return self[key]
        except KeyError:
            self[key] = value
            return value

    def pop(self, key, *args):
        try:
            value = self[key]
            del self[key]
            return value
        except KeyError:
            if len(args) == 1:
                return args[0]
            raise

    def popitem(self):
        (lower_key, value) = super(CIDict, self).popitem()
        key = self._keys[lower_key]
        del self._keys[lower_key]

        return (key, value)

    def clear(self):
        self._keys.clear()
        return super(CIDict, self).clear()

    def viewitems(self):
        raise NotImplementedError('CIDict.viewitems is not implemented')

    def viewkeys(self):
        raise NotImplementedError('CIDict.viewkeys is not implemented')

    def viewvvalues(self):
        raise NotImplementedError('CIDict.viewvvalues is not implemented')


class GeneralizedTimeZone(datetime.tzinfo):
    """This class is a basic timezone wrapper for the offset specified
       in a Generalized Time.  It is dst-ignorant."""
    def __init__(self,offsetstr="Z"):
        super(GeneralizedTimeZone, self).__init__()

        self.name = offsetstr
        self.houroffset = 0
        self.minoffset = 0

        if offsetstr == "Z":
            self.houroffset = 0
            self.minoffset = 0
        else:
            if (len(offsetstr) >= 3) and re.match(r'[-+]\d\d', offsetstr):
                self.houroffset = int(offsetstr[0:3])
                offsetstr = offsetstr[3:]
            if (len(offsetstr) >= 2) and re.match(r'\d\d', offsetstr):
                self.minoffset = int(offsetstr[0:2])
                offsetstr = offsetstr[2:]
            if len(offsetstr) > 0:
                raise ValueError()
        if self.houroffset < 0:
            self.minoffset *= -1

    def utcoffset(self, dt):
        return datetime.timedelta(hours=self.houroffset, minutes=self.minoffset)

    def dst(self):
        return datetime.timedelta(0)

    def tzname(self):
        return self.name


def parse_generalized_time(timestr):
    """Parses are Generalized Time string (as specified in X.680),
       returning a datetime object.  Generalized Times are stored inside
       the krbPasswordExpiration attribute in LDAP.

       This method doesn't attempt to be perfect wrt timezones.  If python
       can't be bothered to implement them, how can we..."""

    if len(timestr) < 8:
        return None
    try:
        date = timestr[:8]
        time = timestr[8:]

        year = int(date[:4])
        month = int(date[4:6])
        day = int(date[6:8])

        hour = min = sec = msec = 0
        tzone = None

        if (len(time) >= 2) and re.match(r'\d', time[0]):
            hour = int(time[:2])
            time = time[2:]
            if len(time) >= 2 and (time[0] == "," or time[0] == "."):
                hour_fraction = "."
                time = time[1:]
                while (len(time) > 0) and re.match(r'\d', time[0]):
                    hour_fraction += time[0]
                    time = time[1:]
                total_secs = int(float(hour_fraction) * 3600)
                min, sec = divmod(total_secs, 60)

        if (len(time) >= 2) and re.match(r'\d', time[0]):
            min = int(time[:2])
            time = time[2:]
            if len(time) >= 2 and (time[0] == "," or time[0] == "."):
                min_fraction = "."
                time = time[1:]
                while (len(time) > 0) and re.match(r'\d', time[0]):
                    min_fraction += time[0]
                    time = time[1:]
                sec = int(float(min_fraction) * 60)

        if (len(time) >= 2) and re.match(r'\d', time[0]):
            sec = int(time[:2])
            time = time[2:]
            if len(time) >= 2 and (time[0] == "," or time[0] == "."):
                sec_fraction = "."
                time = time[1:]
                while (len(time) > 0) and re.match(r'\d', time[0]):
                    sec_fraction += time[0]
                    time = time[1:]
                msec = int(float(sec_fraction) * 1000000)

        if (len(time) > 0):
            tzone = GeneralizedTimeZone(time)

        return datetime.datetime(year, month, day, hour, min, sec, msec, tzone)

    except ValueError:
        return None

def ipa_generate_password(characters=None,pwd_len=None):
    ''' Generates password. Password cannot start or end with a whitespace
    character. It also cannot be formed by whitespace characters only.
    Length of password as well as string of characters to be used by
    generator could be optionaly specified by characters and pwd_len
    parameters, otherwise default values will be used: characters string
    will be formed by all printable non-whitespace characters and space,
    pwd_len will be equal to value of GEN_PWD_LEN.
    '''
    if not characters:
        characters=string.digits + string.ascii_letters + string.punctuation + ' '
    else:
        if characters.isspace():
            raise ValueError("password cannot be formed by whitespaces only")
    if not pwd_len:
        pwd_len = GEN_PWD_LEN

    upper_bound = len(characters) - 1
    rndpwd = ''
    r = random.SystemRandom()

    for x in range(pwd_len):
        rndchar = characters[r.randint(0,upper_bound)]
        if (x == 0) or (x == pwd_len-1):
            while rndchar.isspace():
                rndchar = characters[r.randint(0,upper_bound)]
        rndpwd += rndchar
    return rndpwd

def user_input(prompt, default = None, allow_empty = True):
    if default == None:
        while True:
            try:
                ret = input("%s: " % prompt)
                if allow_empty or ret.strip():
                    return ret.strip()
            except EOFError:
                if allow_empty:
                    return ''
                raise RuntimeError("Failed to get user input")

    if isinstance(default, six.string_types):
        while True:
            try:
                ret = input("%s [%s]: " % (prompt, default))
                if not ret and (allow_empty or default):
                    return default
                elif ret.strip():
                    return ret.strip()
            except EOFError:
                return default

    if isinstance(default, bool):
        choice = "yes" if default else "no"
        while True:
            try:
                ret = input("%s [%s]: " % (prompt, choice))
                ret = ret.strip()
                if not ret:
                    return default
                elif ret.lower()[0] == "y":
                    return True
                elif ret.lower()[0] == "n":
                    return False
            except EOFError:
                return default

    if isinstance(default, int):
        while True:
            try:
                ret = input("%s [%s]: " % (prompt, default))
                ret = ret.strip()
                if not ret:
                    return default
                ret = int(ret)
            except ValueError:
                pass
            except EOFError:
                return default
            else:
                return ret


def host_port_open(host, port, socket_type=socket.SOCK_STREAM, socket_timeout=None):
    for res in socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket_type):
        af, socktype, proto, canonname, sa = res
        try:
            try:
                s = socket.socket(af, socktype, proto)
            except socket.error:
                s = None
                continue

            if socket_timeout is not None:
                s.settimeout(socket_timeout)

            s.connect(sa)

            if socket_type == socket.SOCK_DGRAM:
                s.send('')
                s.recv(512)

            return True
        except socket.error as e:
            pass
        finally:
            if s:
                s.close()

    return False

def bind_port_responder(port, socket_type=socket.SOCK_STREAM, socket_timeout=None, responder_data=None):
    host = None   # all available interfaces
    last_socket_error = None

    # At first try to create IPv6 socket as it is able to accept both IPv6 and
    # IPv4 connections (when not turned off)
    families = (socket.AF_INET6, socket.AF_INET)
    s = None

    for family in families:
        try:
            addr_infos = socket.getaddrinfo(host, port, family, socket_type, 0,
                            socket.AI_PASSIVE)
        except socket.error as e:
            last_socket_error = e
            continue
        for res in addr_infos:
            af, socktype, proto, canonname, sa = res
            try:
                s = socket.socket(af, socktype, proto)
            except socket.error as e:
                last_socket_error = e
                s = None
                continue

            if socket_timeout is not None:
                s.settimeout(1)

            if af == socket.AF_INET6:
                try:
                    # Make sure IPv4 clients can connect to IPv6 socket
                    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                except socket.error:
                    pass

            if socket_type == socket.SOCK_STREAM:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            try:
                s.bind(sa)

                while True:
                    if socket_type == socket.SOCK_STREAM:
                        s.listen(1)
                        connection, client_address = s.accept()
                        try:
                            if responder_data:
                                connection.sendall(responder_data)
                        finally:
                            connection.close()
                    elif socket_type == socket.SOCK_DGRAM:
                        data, addr = s.recvfrom(1)

                        if responder_data:
                            s.sendto(responder_data, addr)
            except socket.timeout:
                # Timeout is expectable as it was requested by caller, raise
                # the exception back to him
                raise
            except socket.error as e:
                last_socket_error = e
                s.close()
                s = None
                continue
            finally:
                if s:
                    s.close()

    if s is None and last_socket_error is not None:
        raise last_socket_error # pylint: disable=E0702


def reverse_record_exists(ip_address):
    """
    Checks if IP address have some reverse record somewhere.
    Does not care where it points.

    Returns True/False
    """
    reverse = reversename.from_address(str(ip_address))
    try:
        resolver.query(reverse, "PTR")
    except DNSException:
        # really don't care what exception, PTR is simply unresolvable
        return False
    return True


def config_replace_variables(filepath, replacevars=dict(), appendvars=dict()):
    """
    Take a key=value based configuration file, and write new version
    with certain values replaced or appended

    All (key,value) pairs from replacevars and appendvars that were not found
    in the configuration file, will be added there.

    It is responsibility of a caller to ensure that replacevars and
    appendvars do not overlap.

    It is responsibility of a caller to back up file.

    returns dictionary of affected keys and their previous values

    One have to run restore_context(filepath) afterwards or
    security context of the file will not be correct after modification
    """
    pattern = re.compile('''
(^
                        \s*
        (?P<option>     [^\#;]+?)
                        (\s*=\s*)
        (?P<value>      .+?)?
                        (\s*((\#|;).*)?)?
$)''', re.VERBOSE)
    orig_stat = os.stat(filepath)
    old_values = dict()
    temp_filename = None
    with tempfile.NamedTemporaryFile(delete=False) as new_config:
        temp_filename = new_config.name
        with open(filepath, 'r') as f:
            for line in f:
                new_line = line
                m = pattern.match(line)
                if m:
                    option, value = m.group('option', 'value')
                    if option is not None:
                        if replacevars and option in replacevars:
                            # replace value completely
                            new_line = u"%s=%s\n" % (option, replacevars[option])
                            old_values[option] = value
                        if appendvars and option in appendvars:
                            # append new value unless it is already existing in the original one
                            if not value:
                                new_line = u"%s=%s\n" % (option, appendvars[option])
                            elif value.find(appendvars[option]) == -1:
                                new_line = u"%s=%s %s\n" % (option, value, appendvars[option])
                            old_values[option] = value
                new_config.write(new_line)
        # Now add all options from replacevars and appendvars that were not found in the file
        new_vars = replacevars.copy()
        new_vars.update(appendvars)
        newvars_view = set(new_vars.keys()) - set(old_values.keys())
        append_view = (set(appendvars.keys()) - newvars_view)
        for item in newvars_view:
            new_config.write("%s=%s\n" % (item,new_vars[item]))
        for item in append_view:
            new_config.write("%s=%s\n" % (item,appendvars[item]))
        new_config.flush()
        # Make sure the resulting file is readable by others before installing it
        os.fchmod(new_config.fileno(), orig_stat.st_mode)
        os.fchown(new_config.fileno(), orig_stat.st_uid, orig_stat.st_gid)

    # At this point new_config is closed but not removed due to 'delete=False' above
    # Now, install the temporary file as configuration and ensure old version is available as .orig
    # While .orig file is not used during uninstall, it is left there for administrator.
    install_file(temp_filename, filepath)

    return old_values

def inifile_replace_variables(filepath, section, replacevars=dict(), appendvars=dict()):
    """
    Take a section-structured key=value based configuration file, and write new version
    with certain values replaced or appended within the section

    All (key,value) pairs from replacevars and appendvars that were not found
    in the configuration file, will be added there.

    It is responsibility of a caller to ensure that replacevars and
    appendvars do not overlap.

    It is responsibility of a caller to back up file.

    returns dictionary of affected keys and their previous values

    One have to run restore_context(filepath) afterwards or
    security context of the file will not be correct after modification
    """
    pattern = re.compile('''
(^
                        \[
        (?P<section>    .+) \]
                        (\s+((\#|;).*)?)?
$)|(^
                        \s*
        (?P<option>     [^\#;]+?)
                        (\s*=\s*)
        (?P<value>      .+?)?
                        (\s*((\#|;).*)?)?
$)''', re.VERBOSE)
    def add_options(config, replacevars, appendvars, oldvars):
        # add all options from replacevars and appendvars that were not found in the file
        new_vars = replacevars.copy()
        new_vars.update(appendvars)
        newvars_view = set(new_vars.keys()) - set(oldvars.keys())
        append_view = (set(appendvars.keys()) - newvars_view)
        for item in newvars_view:
            config.write("%s=%s\n" % (item,new_vars[item]))
        for item in append_view:
            config.write("%s=%s\n" % (item,appendvars[item]))

    orig_stat = os.stat(filepath)
    old_values = dict()
    temp_filename = None
    with tempfile.NamedTemporaryFile(delete=False) as new_config:
        temp_filename = new_config.name
        with open(filepath, 'r') as f:
            in_section = False
            finished = False
            line_idx = 1
            for line in f:
                line_idx = line_idx + 1
                new_line = line
                m = pattern.match(line)
                if m:
                    sect, option, value = m.group('section', 'option', 'value')
                    if in_section and sect is not None:
                        # End of the searched section, add remaining options
                        add_options(new_config, replacevars, appendvars, old_values)
                        finished = True
                    if sect is not None:
                        # New section is found, check whether it is the one we are looking for
                        in_section = (str(sect).lower() == str(section).lower())
                    if option is not None and in_section:
                        # Great, this is an option from the section we are loking for
                        if replacevars and option in replacevars:
                            # replace value completely
                            new_line = u"%s=%s\n" % (option, replacevars[option])
                            old_values[option] = value
                        if appendvars and option in appendvars:
                            # append a new value unless it is already existing in the original one
                            if not value:
                                new_line = u"%s=%s\n" % (option, appendvars[option])
                            elif value.find(appendvars[option]) == -1:
                                new_line = u"%s=%s %s\n" % (option, value, appendvars[option])
                            old_values[option] = value
                    new_config.write(new_line)
            # We have finished parsing the original file.
            # There are two remaining cases:
            # 1. Section we were looking for was not found, we need to add it.
            if not (in_section or finished):
                new_config.write("[%s]\n" % (section))
            # 2. The section is the last one but some options were not found, add them.
            if in_section or not finished:
                add_options(new_config, replacevars, appendvars, old_values)

        new_config.flush()
        # Make sure the resulting file is readable by others before installing it
        os.fchmod(new_config.fileno(), orig_stat.st_mode)
        os.fchown(new_config.fileno(), orig_stat.st_uid, orig_stat.st_gid)

    # At this point new_config is closed but not removed due to 'delete=False' above
    # Now, install the temporary file as configuration and ensure old version is available as .orig
    # While .orig file is not used during uninstall, it is left there for administrator.
    install_file(temp_filename, filepath)

    return old_values

def backup_config_and_replace_variables(
        fstore, filepath, replacevars=dict(), appendvars=dict()):
    """
    Take a key=value based configuration file, back up it, and
    write new version with certain values replaced or appended

    All (key,value) pairs from replacevars and appendvars that
    were not found in the configuration file, will be added there.
    The file must exist before this function is called.

    It is responsibility of a caller to ensure that replacevars and
    appendvars do not overlap.

    returns dictionary of affected keys and their previous values

    One have to run restore_context(filepath) afterwards or
    security context of the file will not be correct after modification
    """
    # Backup original filepath
    fstore.backup_file(filepath)
    old_values = config_replace_variables(filepath, replacevars, appendvars)

    return old_values


def wait_for_open_ports(host, ports, timeout=0):
    """
    Wait until the specified port(s) on the remote host are open. Timeout
    in seconds may be specified to limit the wait. If the timeout is
    exceeded, socket.timeout exception is raised.
    """
    timeout = float(timeout)
    if not isinstance(ports, (tuple, list)):
        ports = [ports]

    root_logger.debug('wait_for_open_ports: %s %s timeout %d', host, ports, timeout)
    op_timeout = time.time() + timeout

    for port in ports:
        while True:
            port_open = host_port_open(host, port)

            if port_open:
                break
            if timeout and time.time() > op_timeout: # timeout exceeded
                raise socket.timeout("Timeout exceeded")
            time.sleep(1)

def wait_for_open_socket(socket_name, timeout=0):
    """
    Wait until the specified socket on the local host is open. Timeout
    in seconds may be specified to limit the wait.
    """
    timeout = float(timeout)
    op_timeout = time.time() + timeout

    while True:
        try:
            s = socket.socket(socket.AF_UNIX)
            s.connect(socket_name)
            s.close()
            break
        except socket.error as e:
            if e.errno in (2,111):  # 111: Connection refused, 2: File not found
                if timeout and time.time() > op_timeout: # timeout exceeded
                    raise e
                time.sleep(1)
            else:
                raise e


def kinit_keytab(principal, keytab, ccache_name, config=None, attempts=1):
    """
    Given a ccache_path, keytab file and a principal kinit as that user.

    The optional parameter 'attempts' specifies how many times the credential
    initialization should be attempted in case of non-responsive KDC.
    """
    errors_to_retry = {KRB5KDC_ERR_SVC_UNAVAILABLE,
                       KRB5_KDC_UNREACH}
    root_logger.debug("Initializing principal %s using keytab %s"
                      % (principal, keytab))
    root_logger.debug("using ccache %s" % ccache_name)
    for attempt in range(1, attempts + 1):
        old_config = os.environ.get('KRB5_CONFIG')
        if config is not None:
            os.environ['KRB5_CONFIG'] = config
        else:
            os.environ.pop('KRB5_CONFIG', None)
        try:
            name = gssapi.Name(principal, gssapi.NameType.kerberos_principal)
            store = {'ccache': ccache_name,
                     'client_keytab': keytab}
            cred = gssapi.Credentials(name=name, store=store, usage='initiate')
            root_logger.debug("Attempt %d/%d: success"
                              % (attempt, attempts))
            return cred
        except gssapi.exceptions.GSSError as e:
            if e.min_code not in errors_to_retry:  # pylint: disable=no-member
                raise
            root_logger.debug("Attempt %d/%d: failed: %s"
                              % (attempt, attempts, e))
            if attempt == attempts:
                root_logger.debug("Maximum number of attempts (%d) reached"
                                  % attempts)
                raise
            root_logger.debug("Waiting 5 seconds before next retry")
            time.sleep(5)
        finally:
            if old_config is not None:
                os.environ['KRB5_CONFIG'] = old_config
            else:
                os.environ.pop('KRB5_CONFIG', None)


def kinit_password(principal, password, ccache_name, config=None,
                   armor_ccache_name=None, canonicalize=False,
                   enterprise=False):
    """
    perform interactive kinit as principal using password. If using FAST for
    web-based authentication, use armor_ccache_path to specify http service
    ccache.
    """
    root_logger.debug("Initializing principal %s using password" % principal)
    args = [paths.KINIT, principal, '-c', ccache_name]
    if armor_ccache_name is not None:
        root_logger.debug("Using armor ccache %s for FAST webauth"
                          % armor_ccache_name)
        args.extend(['-T', armor_ccache_name])

    if canonicalize:
        root_logger.debug("Requesting principal canonicalization")
        args.append('-C')

    if enterprise:
        root_logger.debug("Using enterprise principal")
        args.append('-E')

    env = {'LC_ALL': 'C'}
    if config is not None:
        env['KRB5_CONFIG'] = config

    # this workaround enables us to capture stderr and put it
    # into the raised exception in case of unsuccessful authentication
    result = run(args, stdin=password, env=env, raiseonerr=False,
                 capture_error=True)
    if result.returncode:
        raise RuntimeError(result.error_output)


def dn_attribute_property(private_name):
    '''
    Create a property for a dn attribute which assures the attribute
    is a DN or None. If the value is not None the setter converts it to
    a DN. The getter assures it's either None or a DN instance.

    The private_name parameter is the class internal attribute the property
    shadows.

    For example if a class has an attribute called base_dn, then:

        base_dn = dn_attribute_property('_base_dn')

    Thus the class with have an attriubte called base_dn which can only
    ever be None or a DN instance. The actual value is stored in _base_dn.
    '''

    def setter(self, value):
        if value is not None:
            value = DN(value)
        setattr(self, private_name, value)

    def getter(self):
        value = getattr(self, private_name)
        if value is not None:
            assert isinstance(value, DN)
        return value

    return property(getter, setter)

def posixify(string):
    """
    Convert a string to a more strict alpha-numeric representation.

    - Alpha-numeric, underscore, dot and dash characters are accepted
    - Space is converted to underscore
    - Other characters are omitted
    - Leading dash is stripped

    Note: This mapping is not one-to-one and may map different input to the
    same result. When using posixify, make sure the you do not map two different
    entities to one unintentionally.
    """

    def valid_char(char):
        return char.isalnum() or char in ('_', '.', '-')

    # First replace space characters
    replaced = string.replace(' ','_')
    omitted = ''.join(filter(valid_char, replaced))

    # Leading dash is not allowed
    return omitted.lstrip('-')

@contextmanager
def private_ccache(path=None):

    if path is None:
        dir_path = tempfile.mkdtemp(prefix='krbcc')
        path = os.path.join(dir_path, 'ccache')
    else:
        dir_path = None

    original_value = os.environ.get('KRB5CCNAME', None)

    os.environ['KRB5CCNAME'] = path

    try:
        yield
    finally:
        if original_value is not None:
            os.environ['KRB5CCNAME'] = original_value
        else:
            os.environ.pop('KRB5CCNAME', None)

        if os.path.exists(path):
            os.remove(path)
        if dir_path is not None:
            try:
                os.rmdir(dir_path)
            except OSError:
                pass


if six.PY2:
    def fsdecode(value):
        """
        Decode argument using the file system encoding, as returned by
        `sys.getfilesystemencoding()`.
        """
        if isinstance(value, six.binary_type):
            return value.decode(sys.getfilesystemencoding())
        elif isinstance(value, six.text_type):
            return value
        else:
            raise TypeError("expect {0} or {1}, not {2}".format(
                six.binary_type.__name__,
                six.text_type.__name__,
                type(value).__name__))
else:
    fsdecode = os.fsdecode  #pylint: disable=no-member


def is_fips_enabled():
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
        # Consider that the host is not fips-enabled if the file does not exist
        pass
    return False


def unescape_seq(seq, *args):
    """
    unescape (remove '\\') all occurences of sequence in input strings.

    :param seq: sequence to unescape
    :param args: input string to process

    :returns: tuple of strings with unescaped sequences
    """
    unescape_re = re.compile(r'\\{}'.format(seq))

    return tuple(re.sub(unescape_re, seq, a) for a in args)


def escape_seq(seq, *args):
    """
    escape (prepend '\\') all occurences of sequence in input strings

    :param seq: sequence to escape
    :param args: input string to process

    :returns: tuple of strings with escaped sequences
    """

    return tuple(a.replace(seq, u'\\{}'.format(seq)) for a in args)
