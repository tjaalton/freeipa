#!/usr/bin/env python3
#
# Copyright (C) 2017 FreeIPA Contributors see COPYING for license
#
"""In-tree development server

The dev server requires a Kerberos TGT and a file based credential cache:

    $ mkdir -p ~/.ipa
    $ export KRB5CCNAME=~/.ipa/ccache
    $ kinit admin
    $ make lite-server

Optionally you can set KRB5_CONFIG to use a custom Kerberos configuration
instead of /etc/krb5.conf.

To run the lite-server with another Python interpreter:

    $ make lite-server PYTHON=/path/to/bin/python

To enable profiling:

    $ make lite-server LITESERVER_ARGS='--enable-profiler=-'

By default the dev server supports HTTP only. To switch to HTTPS, you can put
a PEM file at ~/.ipa/lite.pem. The PEM file must contain a server certificate,
its unencrypted private key and intermediate chain certs (if applicable).

Prerequisite
------------

Additionally to build and runtime requirements of FreeIPA, the dev server
depends on the werkzeug framework and optionally watchdog for auto-reloading.
You may also have to enable a development COPR.

    $ sudo dnf install -y dnf-plugins-core
    $ sudo dnf builddep --spec freeipa.spec.in
    $ sudo dnf install -y python3-werkzeug python3-watchdog
    $ ./autogen.sh

For more information see

  * http://www.freeipa.org/page/Build
  * http://www.freeipa.org/page/Testing

"""
from __future__ import print_function

import logging
import os
import optparse  # pylint: disable=deprecated-module
import ssl
import sys
import time
import warnings

import ipalib
from ipalib import api
from ipalib.errors import NetworkError
from ipalib.krb_utils import krb5_parse_ccache
from ipalib.krb_utils import krb5_unparse_ccache

import gssapi
# pylint: disable=import-error
from werkzeug.contrib.profiler import ProfilerMiddleware
from werkzeug.exceptions import NotFound
from werkzeug.serving import run_simple
from werkzeug.utils import redirect, append_slash_redirect
from werkzeug.wsgi import DispatcherMiddleware, SharedDataMiddleware
# pylint: enable=import-error

logger = logging.getLogger(os.path.basename(__file__))


BASEDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMPORTDIR = os.path.dirname(os.path.dirname(os.path.abspath(ipalib.__file__)))

if BASEDIR != IMPORTDIR:
    warnings.warn(
        "ipalib was imported from '{}' instead of '{}'!".format(
            IMPORTDIR, BASEDIR),
        RuntimeWarning
    )

STATIC_FILES = {
    '/ipa/ui': os.path.join(BASEDIR, 'install/ui'),
    '/ipa/ui/js': os.path.join(BASEDIR, 'install/ui/src'),
    '/ipa/ui/js/dojo': os.path.join(BASEDIR, 'install/ui/build/dojo'),
    '/ipa/ui/fonts': '/usr/share/fonts',
}


def get_ccname():
    """Retrieve and validate Kerberos credential cache

    Only FILE schema is supported.
    """
    ccname = os.environ.get('KRB5CCNAME')
    if ccname is None:
        raise ValueError("KRB5CCNAME env var is not set.")
    scheme, location = krb5_parse_ccache(ccname)
    if scheme != 'FILE':  # MEMORY makes no sense
        raise ValueError("Unsupported KRB5CCNAME scheme {}".format(scheme))
    if not os.path.isfile(location):
        raise ValueError("KRB5CCNAME file '{}' does not exit".format(location))
    return krb5_unparse_ccache(scheme, location)


class KRBCheater:
    """Add KRB5CCNAME and GSS_NAME to WSGI environ
    """
    def __init__(self, app, ccname):
        self.app = app
        self.ccname = ccname
        self.creds = gssapi.Credentials(
            usage='initiate',
            store={'ccache': ccname}
        )

    def __call__(self, environ, start_response):
        environ['KRB5CCNAME'] = self.ccname
        environ['GSS_NAME'] = self.creds.name
        return self.app(environ, start_response)


class StaticFilesMiddleware(SharedDataMiddleware):
    def get_directory_loader(self, directory):
        # override directory loader to support index.html
        def loader(path):
            if path is not None:
                path = os.path.join(directory, path)
            else:
                path = directory
            # use index.html for directory views
            if os.path.isdir(path):
                path = os.path.join(path, 'index.html')
            if os.path.isfile(path):
                return os.path.basename(path), self._opener(path)
            return None, None
        return loader


def init_api(ccname):
    """Initialize FreeIPA API from command line
    """
    parser = optparse.OptionParser()

    parser.add_option(
        '--dev',
        help='Run WebUI in development mode',
        default=True,
        action='store_false',
        dest='prod',
    )
    parser.add_option(
        '--host',
        help='Listen on address HOST (default 127.0.0.1)',
        default='127.0.0.1',
    )
    parser.add_option(
        '--port',
        help='Listen on PORT (default 8888)',
        default=8888,
        type='int',
    )
    parser.add_option(
        '--enable-profiler',
        help="Path to WSGI profiler directory or '-' for stderr",
        default=None,
        type='str',
    )

    api.env.in_server = True
    api.env.startup_traceback = True
    # workaround for RefererError in rpcserver
    api.env.in_tree = True
    # workaround: AttributeError: locked: cannot set ldap2.time_limit to None
    api.env.mode = 'production'

    start_time = time.time()
    # pylint: disable=unused-variable
    options, args = api.bootstrap_with_global_options(parser, context='lite')
    api.env._merge(
        lite_port=options.port,
        lite_host=options.host,
        webui_prod=options.prod,
        lite_profiler=options.enable_profiler,
        lite_pem=api.env._join('dot_ipa', 'lite.pem'),
    )
    api.finalize()
    api_time = time.time()
    logger.info("API initialized in %03f sec", api_time - start_time)

    # Validate LDAP connection and pre-fetch schema
    # Pre-fetching makes the lite-server behave similar to mod_wsgi. werkzeug's
    # multi-process WSGI server forks a new process for each request while
    # mod_wsgi handles multiple request in a daemon process. Without schema
    # cache, every lite server request would download the LDAP schema and
    # distort performance profiles.
    ldap2 = api.Backend.ldap2
    try:
        if not ldap2.isconnected():
            ldap2.connect(ccache=ccname)
    except NetworkError as e:
        logger.error("Unable to connect to LDAP: %s", e)
        logger.error("lite-server needs a working LDAP connect. Did you "
                     "configure ldap_uri in '%s'?", api.env.conf_default)
        sys.exit(2)
    else:
        # prefetch schema
        assert ldap2.schema
        # Disconnect main process, each WSGI request handler subprocess will
        # must have its own connection.
        ldap2.disconnect()
        ldap_time = time.time()
        logger.info("LDAP schema retrieved %03f sec", ldap_time - api_time)


def redirect_ui(app):
    """Redirects for UI
    """
    def wsgi(environ, start_response):
        path_info = environ['PATH_INFO']
        if path_info in {'/', '/ipa', '/ipa/'}:
            response = redirect('/ipa/ui/')
            return response(environ, start_response)
        # Redirect to append slash to some routes
        if path_info in {'/ipa/ui', '/ipa/ui/test'}:
            response = append_slash_redirect(environ)
            return response(environ, start_response)
        if path_info == '/favicon.ico':
            response = redirect('/ipa/ui/favicon.ico')
            return response(environ, start_response)
        return app(environ, start_response)
    return wsgi


def main():
    try:
        ccname = get_ccname()
    except ValueError as e:
        print("ERROR:", e, file=sys.stderr)
        print("\nliteserver requires a KRB5CCNAME env var and "
              "a valid Kerberos TGT:\n", file=sys.stderr)
        print("    export KRB5CCNAME=~/.ipa/ccache", file=sys.stderr)
        print("    kinit\n", file=sys.stderr)
        sys.exit(1)

    init_api(ccname)

    if os.path.isfile(api.env.lite_pem):
        ctx = ssl.create_default_context(purpose=ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(api.env.lite_pem)
    else:
        ctx = None

    app = NotFound()
    app = DispatcherMiddleware(app, {
        '/ipa': KRBCheater(api.Backend.wsgi_dispatch, ccname),
    })

    # only profile api calls
    if api.env.lite_profiler == '-':
        print('Profiler enable, stats are written to stderr.')
        app = ProfilerMiddleware(app, stream=sys.stderr, restrictions=(30,))
    elif api.env.lite_profiler:
        profile_dir = os.path.abspath(api.env.lite_profiler)
        print("Profiler enable, profiles are stored in '{}'.".format(
            profile_dir
        ))
        app = ProfilerMiddleware(app, profile_dir=profile_dir)

    app = StaticFilesMiddleware(app, STATIC_FILES)
    app = redirect_ui(app)

    run_simple(
        hostname=api.env.lite_host,
        port=api.env.lite_port,
        application=app,
        processes=5,
        ssl_context=ctx,
        use_reloader=True,
        # debugger doesn't work because framework catches all exceptions
        # use_debugger=not api.env.webui_prod,
        # use_evalex=not api.env.webui_prod,
    )

if __name__ == '__main__':
    main()
