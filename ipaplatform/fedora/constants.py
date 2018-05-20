#
# Copyright (C) 2015  FreeIPA Contributors see COPYING for license
#

'''
This Fedora base platform module exports platform related constants.
'''

# Fallback to default constant definitions
from __future__ import absolute_import

from ipaplatform.redhat.constants import RedHatConstantsNamespace


class FedoraConstantsNamespace(RedHatConstantsNamespace):
    # Fedora allows installation of Python 2 and 3 mod_wsgi, but the modules
    # can't coexist. For Apache to load correct module.
    MOD_WSGI_PYTHON2 = "modules/mod_wsgi.so"
    MOD_WSGI_PYTHON3 = "modules/mod_wsgi_python3.so"

    # System-wide crypto policy, but without TripleDES, pre-shared key,
    # secure remote password, and DSA cert authentication.
    # see https://fedoraproject.org/wiki/Changes/CryptoPolicy
    TLS_HIGH_CIPHERS = "PROFILE=SYSTEM:!3DES:!PSK:!SRP:!aDSS"


constants = FedoraConstantsNamespace()
