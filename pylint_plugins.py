#
# Copyright (C) 2015  FreeIPA Contributors see COPYING for license
#

from __future__ import print_function

import copy
import sys

from astroid import MANAGER
from astroid import scoped_nodes


def register(linter):
    pass


def _warning_already_exists(cls, member):
    print(
        "WARNING: member '{member}' in '{cls}' already exists".format(
            cls="{}.{}".format(cls.root().name, cls.name), member=member),
        file=sys.stderr
    )


def fake_class(name_or_class_obj, members=()):
    if isinstance(name_or_class_obj, scoped_nodes.Class):
        cl = name_or_class_obj
    else:
        cl = scoped_nodes.Class(name_or_class_obj, None)

    for m in members:
        if isinstance(m, str):
            if m in cl.locals:
                _warning_already_exists(cl, m)
            else:
                cl.locals[m] = [scoped_nodes.Class(m, None)]
        elif isinstance(m, dict):
            for key, val in m.items():
                assert isinstance(key, str), "key must be string"
                if key in cl.locals:
                    _warning_already_exists(cl, key)
                    fake_class(cl.locals[key], val)
                else:
                    cl.locals[key] = [fake_class(key, val)]
        else:
            # here can be used any astroid type
            if m.name in cl.locals:
                _warning_already_exists(cl, m.name)
            else:
                cl.locals[m.name] = [copy.copy(m)]
    return cl


fake_backend = {'Backend': [
    {'wsgi_dispatch': ['mount']},
]}

NAMESPACE_ATTRS = ['Command', 'Object', 'Method', fake_backend, 'Updater',
                   'Advice']
fake_api_env = {'env': [
    'host',
    'realm',
    'session_auth_duration',
    'session_duration_type',
]}

# this is due ipaserver.rpcserver.KerberosSession where api is undefined
fake_api = {'api': [fake_api_env] + NAMESPACE_ATTRS}

_LOGGING_ATTRS = ['debug', 'info', 'warning', 'error', 'exception',
                  'critical', 'warn']
LOGGING_ATTRS = [
    {'log': _LOGGING_ATTRS},
] + _LOGGING_ATTRS

# 'class': ['generated', 'properties']
ipa_class_members = {
    # Python standard library & 3rd party classes
    'socket._socketobject': ['sendall'],

    # IPA classes
    'ipalib.base.NameSpace': [
        'add',
        'mod',
        'del',
        'show',
        'find'
    ],
    'ipalib.cli.Collector': ['__options'],
    'ipalib.config.Env': [
        {'__d': ['get']},
        {'__done': ['add']},
        'xmlrpc_uri',
        'validate_api',
        'startup_traceback',
        'verbose'
    ] + LOGGING_ATTRS,
    'ipalib.errors.ACIError': [
        'info',
    ],
    'ipalib.errors.ConversionError': [
        'error',
    ],
    'ipalib.errors.DatabaseError': [
        'desc',
    ],
    'ipalib.errors.NetworkError': [
        'error',
    ],
    'ipalib.errors.NotFound': [
        'reason',
    ],
    'ipalib.errors.PublicError': [
        'msg',
        'strerror',
    ],
    'ipalib.errors.SingleMatchExpected': [
        'found',
    ],
    'ipalib.errors.SkipPluginModule': [
        'reason',
    ],
    'ipalib.errors.ValidationError': [
        'error',
    ],
    'ipalib.messages.PublicMessage': [
        'msg',
        'strerror',
        'type',
    ],
    'ipalib.parameters.Param': [
        'cli_name',
        'cli_short_name',
        'label',
        'default',
        'doc',
        'required',
        'multivalue',
        'primary_key',
        'normalizer',
        'default_from',
        'autofill',
        'query',
        'attribute',
        'include',
        'exclude',
        'flags',
        'hint',
        'alwaysask',
        'sortorder',
        'csv',
        'option_group',
     ],
    'ipalib.parameters.Bool': [
        'truths',
        'falsehoods'],
    'ipalib.parameters.Data': [
        'minlength',
        'maxlength',
        'length',
        'pattern',
        'pattern_errmsg',
    ],
    'ipalib.parameters.Str': ['noextrawhitespace'],
    'ipalib.parameters.Password': ['confirm'],
    'ipalib.parameters.File': ['stdin_if_missing'],
    'ipalib.plugins.dns.DNSRecord': [
        'validatedns',
        'normalizedns',
    ],
    'ipalib.parameters.Enum': ['values'],
    'ipalib.parameters.Number': [
        'minvalue',
        'maxvalue',
    ],
    'ipalib.parameters.Decimal': [
        'precision',
        'exponential',
        'numberclass',
    ],
    'ipalib.parameters.DNSNameParam': [
        'only_absolute',
        'only_relative',
    ],
    'ipalib.plugable.API': [
        fake_api_env,
    ] + NAMESPACE_ATTRS + LOGGING_ATTRS,
    'ipalib.plugable.Plugin': [
        'Object',
        'Method',
        'Updater',
        'Advice',
    ] + LOGGING_ATTRS,
    'ipalib.session.AuthManager': LOGGING_ATTRS,
    'ipalib.session.SessionAuthManager': LOGGING_ATTRS,
    'ipalib.session.SessionManager': LOGGING_ATTRS,
    'ipalib.util.ForwarderValidationError': [
        'msg',
    ],
    'ipaserver.install.ldapupdate.LDAPUpdate': LOGGING_ATTRS,
    'ipaserver.rpcserver.KerberosSession': [
        fake_api,
    ] + LOGGING_ATTRS,
    'ipatests.test_integration.base.IntegrationTest': [
        'domain',
        {'master': [
            {'config': [
                {'dirman_password': dir(str)},
                {'admin_password': dir(str)},
                {'admin_name': dir(str)},
                {'dns_forwarder': dir(str)},
                {'test_dir': dir(str)},
                {'ad_admin_name': dir(str)},
                {'ad_admin_password': dir(str)},
                {'domain_level': dir(str)},
            ]},
            {'domain': [
                {'realm': dir(str)},
                {'name': dir(str)},
            ]},
            'hostname',
            'ip',
            'collect_log',
            {'run_command': [
                {'stdout_text': dir(str)},
                'stderr_text',
                'returncode',
            ]},
            {'transport': ['put_file']},
            'put_file_contents',
            'get_file_contents',
            'ldap_connect',
        ]},
        'replicas',
        'clients',
        'ad_domains',
    ]
}


def fix_ipa_classes(cls):
    class_name_with_module = "{}.{}".format(cls.root().name, cls.name)
    if class_name_with_module in ipa_class_members:
        fake_class(cls, ipa_class_members[class_name_with_module])

MANAGER.register_transform(scoped_nodes.Class, fix_ipa_classes)
