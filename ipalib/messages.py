# Authors:
#   Petr Viktorin <pviktori@redhat.com>
#
# Copyright (C) 2012  Red Hat
# see file 'COPYING' for use and warranty inmsgion
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
Custom message (debug, info, wraning) classes passed through RPC.

These are added to the "messages" entry in a RPC response, and printed to the
user as log messages.

Each message class has a unique numeric "errno" attribute from the 10000-10999
range, so that it does not clash with PublicError numbers.

Messages also have the 'type' argument, set to one of 'debug', 'info',
'warning', 'error'. This determines the severity of themessage.
"""

from inspect import isclass

from ipalib.constants import TYPE_ERROR
from ipalib.text import _ as ugettext
from ipalib.text import Gettext, NGettext
from ipalib.capabilities import client_has_capability


def add_message(version, result, message):
    if client_has_capability(version, 'messages'):
        result.setdefault('messages', []).append(message.to_dict())


def process_message_arguments(obj, format=None, message=None, **kw):
    obj.kw = kw
    name = obj.__class__.__name__
    if obj.format is not None and format is not None:
        raise ValueError(
            'non-generic %r needs format=None; got format=%r' % (
                name, format)
        )
    if message is None:
        if obj.format is None:
            if format is None:
                raise ValueError(
                    '%s.format is None yet format=None, message=None' % name
                )
            obj.format = format
        obj.forwarded = False
        obj.msg = obj.format % kw
        if isinstance(obj.format, basestring):
            obj.strerror = ugettext(obj.format) % kw
        else:
            obj.strerror = obj.format % kw
        if 'instructions' in kw:
            def convert_instructions(value):
                if isinstance(value, list):
                    result = u'\n'.join(map(lambda line: unicode(line), value))
                    return result
                return value
            instructions = u'\n'.join((unicode(_('Additional instructions:')),
                                    convert_instructions(kw['instructions'])))
            obj.strerror = u'\n'.join((obj.strerror, instructions))
    else:
        if isinstance(message, (Gettext, NGettext)):
            message = unicode(message)
        elif type(message) is not unicode:
            raise TypeError(
                TYPE_ERROR % ('message', unicode, message, type(message))
            )
        obj.forwarded = True
        obj.msg = message
        obj.strerror = message
    for (key, value) in kw.iteritems():
        assert not hasattr(obj, key), 'conflicting kwarg %s.%s = %r' % (
            name, key, value,
        )
        setattr(obj, key, value)


_texts = []

def _(message):
    _texts.append(message)
    return message


class PublicMessage(UserWarning):
    """
    **10000** Base class for messages that can be forwarded in an RPC response.
    """
    def __init__(self, format=None, message=None, **kw):
        process_message_arguments(self, format, message, **kw)
        super(PublicMessage, self).__init__(self.msg)

    errno = 10000
    format = None

    def to_dict(self):
        """Export this message to a dict that can be sent through RPC"""
        return dict(
            type=unicode(self.type),
            name=unicode(type(self).__name__),
            message=self.strerror,
            code=self.errno,
        )


class VersionMissing(PublicMessage):
    """
    **13001** Used when client did not send the API version.

    For example:

    >>> VersionMissing(server_version='2.123').strerror
    u"API Version number was not sent, forward compatibility not guaranteed. Assuming server's API version, 2.123"

    """

    errno = 13001
    type = 'warning'
    format = _("API Version number was not sent, forward compatibility not "
        "guaranteed. Assuming server's API version, %(server_version)s")


class ForwardersWarning(PublicMessage):
    """
    **13002** Used when (master) zone contains forwarders
    """

    errno = 13002
    type = 'warning'
    format =  _(
        u"DNS forwarder semantics changed since IPA 4.0.\n"
        u"You may want to use forward zones (dnsforwardzone-*) instead.\n"
        u"For more details read the docs.")


class DNSSECWarning(PublicMessage):
    """
    **13003** Used when user change DNSSEC settings
    """

    errno = 13003
    type = "warning"
    format = _("DNSSEC support is experimental.\n%(additional_info)s")

def iter_messages(variables, base):
    """Return a tuple with all subclasses
    """
    for (key, value) in variables.items():
        if key.startswith('_') or not isclass(value):
            continue
        if issubclass(value, base):
            yield value


public_messages = tuple(sorted(
    iter_messages(globals(), PublicMessage), key=lambda E: E.errno))

def print_report(label, classes):
    for cls in classes:
        print '%d\t%s' % (cls.errno, cls.__name__)
    print '(%d %s)' % (len(classes), label)

if __name__ == '__main__':
    print_report('public messages', public_messages)
