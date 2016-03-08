#
# Copyright (C) 2015  FreeIPA Contributors see COPYING for license
#

from __future__ import print_function

import os
import sys

import six

from ipapython.dn import DN
from ipapython.install import common, core
from ipapython.install.core import Knob
from ipalib.util import validate_domain_name
from ipaserver.install import bindinstance
from ipapython.ipautil import check_zone_overlap
from ipapython.dnsutil import DNSName

if six.PY3:
    unicode = str

VALID_SUBJECT_ATTRS = ['st', 'o', 'ou', 'dnqualifier', 'c',
                       'serialnumber', 'l', 'title', 'sn', 'givenname',
                       'initials', 'generationqualifier', 'dc', 'mail',
                       'uid', 'postaladdress', 'postalcode', 'postofficebox',
                       'houseidentifier', 'e', 'street', 'pseudonym',
                       'incorporationlocality', 'incorporationstate',
                       'incorporationcountry', 'businesscategory']


class BaseServerCA(common.Installable, core.Group, core.Composite):
    description = "certificate system"

    external_ca = Knob(
        bool, False,
        description=("Generate a CSR for the IPA CA certificate to be signed "
                     "by an external CA"),
    )

    external_ca_type = Knob(
        {'generic', 'ms-cs'}, None,
        description="Type of the external CA",
    )

    external_cert_files = Knob(
        (list, str), None,
        description=("File containing the IPA CA certificate and the external "
                     "CA certificate chain"),
        cli_name='external-cert-file',
        cli_aliases=['external_cert_file', 'external_ca_file'],
        cli_metavar='FILE',
    )

    @external_cert_files.validator
    def external_cert_files(self, value):
        if any(not os.path.isabs(path) for path in value):
            raise ValueError("must use an absolute path")

    dirsrv_cert_files = Knob(
        (list, str), None,
        description=("File containing the Directory Server SSL certificate "
                     "and private key"),
        cli_name='dirsrv-cert-file',
        cli_metavar='FILE',
    )

    http_cert_files = Knob(
        (list, str), None,
        description=("File containing the Apache Server SSL certificate and "
                     "private key"),
        cli_name='http-cert-file',
        cli_metavar='FILE',
    )

    pkinit_cert_files = Knob(
        (list, str), None,
        description=("File containing the Kerberos KDC SSL certificate and "
                     "private key"),
        cli_name='pkinit-cert-file',
        cli_metavar='FILE',
    )

    dirsrv_pin = Knob(
        str, None,
        sensitive=True,
        description="The password to unlock the Directory Server private key",
        cli_metavar='PIN',
    )

    http_pin = Knob(
        str, None,
        sensitive=True,
        description="The password to unlock the Apache Server private key",
        cli_metavar='PIN',
    )

    pkinit_pin = Knob(
        str, None,
        sensitive=True,
        description="The password to unlock the Kerberos KDC private key",
        cli_metavar='PIN',
    )

    dirsrv_cert_name = Knob(
        str, None,
        description="Name of the Directory Server SSL certificate to install",
        cli_metavar='NAME',
    )

    http_cert_name = Knob(
        str, None,
        description="Name of the Apache Server SSL certificate to install",
        cli_metavar='NAME',
    )

    pkinit_cert_name = Knob(
        str, None,
        description="Name of the Kerberos KDC SSL certificate to install",
        cli_metavar='NAME',
    )

    ca_cert_files = Knob(
        (list, str), None,
        description=("File containing CA certificates for the service "
                     "certificate files"),
        cli_name='ca-cert-file',
        cli_aliases=['root-ca-file'],
        cli_metavar='FILE',
    )

    subject = Knob(
        str, None,
        description="The certificate subject base (default O=<realm-name>)",
    )

    @subject.validator
    def subject(self, value):
        v = unicode(value, 'utf-8')
        if any(ord(c) < 0x20 for c in v):
            raise ValueError("must not contain control characters")
        if '&' in v:
            raise ValueError("must not contain an ampersand (\"&\")")
        try:
            dn = DN(v)
            for rdn in dn:
                if rdn.attr.lower() not in VALID_SUBJECT_ATTRS:
                    raise ValueError("invalid attribute: \"%s\"" % rdn.attr)
        except ValueError as e:
            raise ValueError("invalid subject base format: %s" % e)

    ca_signing_algorithm = Knob(
        {'SHA1withRSA', 'SHA256withRSA', 'SHA512withRSA'}, None,
        description="Signing algorithm of the IPA CA certificate",
    )

    skip_schema_check = Knob(
        bool, False,
        description="skip check for updated CA DS schema on the remote master",
    )


class BaseServerDNS(common.Installable, core.Group, core.Composite):
    description = "DNS"

    forwarders = Knob(
        (list, 'ip'), None,
        description=("Add a DNS forwarder. This option can be used multiple "
                     "times"),
        cli_name='forwarder',
    )

    auto_forwarders = Knob(
        bool, False,
        description="Use DNS forwarders configured in /etc/resolv.conf",
    )

    no_forwarders = Knob(
        bool, False,
        description="Do not add any DNS forwarders, use root servers instead",
    )

    allow_zone_overlap = Knob(
        bool, False,
        description="Create DNS zone even if it already exists",
    )

    reverse_zones = Knob(
        (list, str), [],
        description=("The reverse DNS zone to use. This option can be used "
                     "multiple times"),
        cli_name='reverse-zone',
        cli_metavar='REVERSE_ZONE',
    )

    @reverse_zones.validator
    def reverse_zones(self, values):
        if not self.allow_zone_overlap:
            for zone in values:
                check_zone_overlap(zone)

    no_reverse = Knob(
        bool, False,
        description="Do not create new reverse DNS zone",
    )

    auto_reverse = Knob(
        bool, False,
        description="Create necessary reverse zones",
    )

    no_dnssec_validation = Knob(
        bool, False,
        description="Disable DNSSEC validation",
    )

    dnssec_master = Knob(
        bool, False,
        description="Setup server to be DNSSEC key master",
    )

    disable_dnssec_master = Knob(
        bool, False,
        description="Disable the DNSSEC master on this server",
    )

    kasp_db_file = Knob(
        str, None,
        description="Copy OpenDNSSEC metadata from the specified file (will "
                    "not create a new kasp.db file)",
    )

    force = Knob(
        bool, False,
        description="Force install",
    )

    zonemgr = Knob(
        str, None,
        description=("DNS zone manager e-mail address. Defaults to "
                     "hostmaster@DOMAIN"),
    )

    @zonemgr.validator
    def zonemgr(self, value):
        # validate the value first
        try:
            # IDNA support requires unicode
            encoding = getattr(sys.stdin, 'encoding', None)
            if encoding is None:
                encoding = 'utf-8'
            value = value.decode(encoding)
            bindinstance.validate_zonemgr_str(value)
        except ValueError as e:
            # FIXME we can do this in better way
            # https://fedorahosted.org/freeipa/ticket/4804
            # decode to proper stderr encoding
            stderr_encoding = getattr(sys.stderr, 'encoding', None)
            if stderr_encoding is None:
                stderr_encoding = 'utf-8'
            error = unicode(e).encode(stderr_encoding)
            raise ValueError(error)


class BaseServer(common.Installable, common.Interactive, core.Composite):
    realm_name = Knob(
        str, None,
        description="realm name",
        cli_name='realm',
        cli_short_name='r',
    )

    domain_name = Knob(
        str, None,
        description="domain name",
        cli_name='domain',
        cli_short_name='n',
    )

    @domain_name.validator
    def domain_name(self, value):
        validate_domain_name(value)
        if (self.setup_dns and
                not self.dns.allow_zone_overlap):  # pylint: disable=no-member
            print("Checking DNS domain %s, please wait ..." % value)
            check_zone_overlap(value, False)


    dm_password = Knob(
        str, None,
        sensitive=True,
        cli_short_name='p',
    )

    admin_password = Knob(
        str, None,
        sensitive=True,
    )

    mkhomedir = Knob(
        bool, False,
        description="create home directories for users on their first login",
    )

    host_name = Knob(
        str, None,
        description="fully qualified name of this host",
        cli_name='hostname',
    )

    ip_addresses = Knob(
        (list, 'ip-local'), None,
        description=("Master Server IP Address. This option can be used "
                     "multiple times"),
        cli_name='ip-address',
        cli_metavar='IP_ADDRESS',
    )

    no_host_dns = Knob(
        bool, False,
        description="Do not use DNS for hostname lookup during installation",
    )

    setup_ca = Knob(
        bool, False,
        description="configure a dogtag CA",
    )

    setup_kra = Knob(
        bool, False,
        description="configure a dogtag KRA",
    )

    setup_dns = Knob(
        bool, False,
        description="configure bind with our zone",
    )

    no_ntp = Knob(
        bool, False,
        description="do not configure ntp",
        cli_short_name='N',
    )

    no_pkinit = Knob(
        bool, False,
        description="disables pkinit setup steps",
    )

    no_ui_redirect = Knob(
        bool, False,
        description="Do not automatically redirect to the Web UI",
    )

    ssh_trust_dns = Knob(
        bool, False,
        description="configure OpenSSH client to trust DNS SSHFP records",
    )

    no_ssh = Knob(
        bool, False,
        description="do not configure OpenSSH client",
    )

    no_sshd = Knob(
        bool, False,
        description="do not configure OpenSSH server",
    )

    no_dns_sshfp = Knob(
        bool, False,
        description="Do not automatically create DNS SSHFP records",
    )

    dirsrv_config_file = Knob(
        str, None,
        description="The path to LDIF file that will be used to modify "
                    "configuration of dse.ldif during installation of the "
                    "directory server instance",
        cli_metavar='FILE',
    )

    @dirsrv_config_file.validator
    def dirsrv_config_file(self, value):
        if not os.path.exists(value):
            raise ValueError("File %s does not exist." % value)


    def __init__(self, **kwargs):
        super(BaseServer, self).__init__(**kwargs)

        #pylint: disable=no-member

        # If any of the key file options are selected, all are required.
        cert_file_req = (self.ca.dirsrv_cert_files, self.ca.http_cert_files)
        cert_file_opt = (self.ca.pkinit_cert_files,)
        if any(cert_file_req + cert_file_opt) and not all(cert_file_req):
            raise RuntimeError(
                "--dirsrv-cert-file and --http-cert-file are required if any "
                "key file options are used.")

        if not self.interactive:
            if self.ca.dirsrv_cert_files and self.ca.dirsrv_pin is None:
                raise RuntimeError(
                    "You must specify --dirsrv-pin with --dirsrv-cert-file")
            if self.ca.http_cert_files and self.ca.http_pin is None:
                raise RuntimeError(
                    "You must specify --http-pin with --http-cert-file")
            if self.ca.pkinit_cert_files and self.ca.pkinit_pin is None:
                raise RuntimeError(
                    "You must specify --pkinit-pin with --pkinit-cert-file")

        if self.ca.external_cert_files and self.ca.dirsrv_cert_files:
            raise RuntimeError(
                "Service certificate file options cannot be used with the "
                "external CA options.")

        if self.ca.external_ca_type and not self.ca.external_ca:
            raise RuntimeError(
                "You cannot specify --external-ca-type without --external-ca")

        if not self.setup_dns:
            if self.dns.forwarders:
                raise RuntimeError(
                    "You cannot specify a --forwarder option without the "
                    "--setup-dns option")
            if self.dns.auto_forwarders:
                raise RuntimeError(
                    "You cannot specify a --auto-forwarders option without "
                    "the --setup-dns option")
            if self.dns.no_forwarders:
                raise RuntimeError(
                    "You cannot specify a --no-forwarders option without the "
                    "--setup-dns option")
            if self.dns.reverse_zones:
                raise RuntimeError(
                    "You cannot specify a --reverse-zone option without the "
                    "--setup-dns option")
            if self.dns.auto_reverse:
                raise RuntimeError(
                    "You cannot specify a --auto-reverse option without the "
                    "--setup-dns option")
            if self.dns.no_reverse:
                raise RuntimeError(
                    "You cannot specify a --no-reverse option without the "
                    "--setup-dns option")
            if self.dns.no_dnssec_validation:
                raise RuntimeError(
                    "You cannot specify a --no-dnssec-validation option "
                    "without the --setup-dns option")
        elif self.dns.forwarders and self.dns.no_forwarders:
            raise RuntimeError(
                "You cannot specify a --forwarder option together with "
                "--no-forwarders")
        elif self.dns.auto_forwarders and self.dns.no_forwarders:
            raise RuntimeError(
                "You cannot specify a --auto-forwarders option together with "
                "--no-forwarders")
        elif self.dns.reverse_zones and self.dns.no_reverse:
            raise RuntimeError(
                "You cannot specify a --reverse-zone option together with "
                "--no-reverse")
        elif self.dns.auto_reverse and self.dns.no_reverse:
            raise RuntimeError(
                "You cannot specify a --auto-reverse option together with "
                "--no-reverse")

        # Automatically disable pkinit w/ dogtag until that is supported
        self.no_pkinit = True

        self.unattended = not self.interactive

    ca = core.Component(BaseServerCA)
    dns = core.Component(BaseServerDNS)
