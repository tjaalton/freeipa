# Authors:
#    Alexander Bokovoy <abokovoy@redhat.com>
#
# Copyright (C) 2012  Red Hat
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
Test the `ipalib/plugins/idrange.py` module, and XML-RPC in general.
"""

from ipalib import api, errors
from xmlrpc_test import Declarative, fuzzy_uuid
from ipatests.test_xmlrpc import objectclasses
from ipatests.util import MockLDAP
from ipapython.dn import DN

id_shift = 0
rid_shift = 0

for idrange in api.Command['idrange_find']()['result']:
    size = int(idrange['ipaidrangesize'][0])
    base_id = int(idrange['ipabaseid'][0])

    id_end = base_id + size
    rid_end = 0

    if 'ipabaserid' in idrange:
        base_rid = int(idrange['ipabaserid'][0])
        rid_end = base_rid + size

    if 'ipasecondarybaserid' in idrange:
        secondary_base_rid = int(idrange['ipasecondarybaserid'][0])
        rid_end = max(base_rid, secondary_base_rid) + size

    if id_shift < id_end:
        id_shift = id_end + 1000000

    if rid_shift < rid_end:
        rid_shift = rid_end + 1000000

testrange1 = u'testrange1'
testrange1_base_id = id_shift + 900000
testrange1_size = 99999
testrange1_base_rid = rid_shift + 10000
testrange1_secondary_base_rid = rid_shift + 200000

testrange2 = u'testrange2'
testrange2_base_id = id_shift + 100
testrange2_size = 50
testrange2_base_rid = rid_shift + 100
testrange2_secondary_base_rid = rid_shift + 1000

testrange3 = u'testrange3'
testrange3_base_id = id_shift + 200
testrange3_size = 50
testrange3_base_rid = rid_shift + 70
testrange3_secondary_base_rid = rid_shift + 1100

testrange4 = u'testrange4'
testrange4_base_id = id_shift + 300
testrange4_size = 50
testrange4_base_rid = rid_shift + 200
testrange4_secondary_base_rid = rid_shift + 1030

testrange5 = u'testrange5'
testrange5_base_id = id_shift + 400
testrange5_size = 50
testrange5_base_rid = rid_shift + 1020
testrange5_secondary_base_rid = rid_shift + 1200

testrange6 = u'testrange6'
testrange6_base_id = id_shift + 130
testrange6_size = 50
testrange6_base_rid = rid_shift + 500
testrange6_secondary_base_rid = rid_shift + 1300

testrange7 = u'testrange7'
testrange7_base_id = id_shift + 600
testrange7_size = 50
testrange7_base_rid = rid_shift + 600
testrange7_secondary_base_rid = rid_shift + 649

testrange8 = u'testrange8'
testrange8_base_id = id_shift + 700
testrange8_size = 50
testrange8_base_rid = rid_shift + 700
testrange8_secondary_base_rid = rid_shift + 800

testrange9 = u'testrange9'
testrange9_base_id = id_shift + 800
testrange9_size = 50
testrange9_base_rid = rid_shift + 800

testrange10 = u'testrange10'
testrange10_base_id = id_shift + 900
testrange10_size = 50
testrange10_base_rid = rid_shift + 900

testrange9_dn = "cn={name},cn=ranges,cn=etc,{basedn}".format(name=testrange9,
                                                      basedn=api.env.basedn)

testrange9_add = dict(
    objectClass=["ipaIDrange", "ipatrustedaddomainrange"],
    ipaBaseID="{base_id}".format(base_id=testrange9_base_id),
    ipaBaseRID="{base_rid}".format(base_rid=testrange9_base_rid),
    ipaIDRangeSize="{size}".format(size=testrange9_size),
    ipaNTTrustedDomainSID="S-1-5-21-259319770-2312917334-591429603",
    ipaRangeType="ipa-ad-trust",
    )

testrange10_dn = "cn={name},cn=ranges,cn=etc,{basedn}".format(name=testrange10,
                                                       basedn=api.env.basedn)

testrange10_add = dict(
    objectClass=["ipaIDrange", "ipatrustedaddomainrange"],
    ipaBaseID="{base_id}".format(base_id=testrange10_base_id),
    ipaBaseRID="{base_rid}".format(base_rid=testrange10_base_rid),
    ipaIDRangeSize="{size}".format(size=testrange10_size),
    ipaNTTrustedDomainSID="S-1-5-21-2997650941-1802118864-3094776726",
    ipaRangeType="ipa-ad-trust",
    )

testtrust = u'testtrust'
testtrust_dn = "cn=testtrust,cn=trusts,{basedn}".format(basedn=api.env.basedn)

testtrust_add = dict(
    objectClass=["ipaNTTrustedDomain", "ipaIDobject", "top"],
    ipaNTFlatName='TESTTRUST',
    ipaNTTrustedDomainSID='S-1-5-21-2997650941-1802118864-3094776726',
    ipaNTSIDBlacklistIncoming='S-1-0',
    ipaNTTrustPartner='testtrust.mock',
    ipaNTTrustType='2',
    ipaNTTrustDirection='3',
    ipaNTTrustAttributes='8',
    )

user1 = u'tuser1'
user1_uid = id_shift + 900000
group1 = u'group1'
group1_gid = id_shift + 900100


class test_range(Declarative):
    @classmethod
    def setUpClass(cls):
        super(test_range, cls).setUpClass()
        cls.tearDownClass()
        cls.mockldap = MockLDAP()
        cls.mockldap.add_entry(testrange9_dn, testrange9_add)
        cls.mockldap.add_entry(testrange10_dn, testrange10_add)
        cls.mockldap.add_entry(testtrust_dn, testtrust_add)
        cls.mockldap.unbind()

    @classmethod
    def tearDownClass(cls):
        cls.mockldap = MockLDAP()
        cls.mockldap.del_entry(testrange9_dn)
        cls.mockldap.del_entry(testrange10_dn)
        cls.mockldap.del_entry(testtrust_dn)
        cls.mockldap.unbind()

    cleanup_commands = [
        ('idrange_del', [testrange1, testrange2, testrange3, testrange4,
                         testrange5, testrange6, testrange7, testrange8],
                        {'continue': True}),
        ('user_del', [user1], {}),
        ('group_del', [group1], {}),
    ]

    tests = [
        dict(
            desc='Create ID range %r' % (testrange1),
            command=('idrange_add', [testrange1],
                      dict(ipabaseid=testrange1_base_id, ipaidrangesize=testrange1_size,
                           ipabaserid=testrange1_base_rid, ipasecondarybaserid=testrange1_secondary_base_rid)),
            expected=dict(
                result=dict(
                    dn=DN(('cn',testrange1),('cn','ranges'),('cn','etc'),
                          api.env.basedn),
                    cn=[testrange1],
                    objectclass=[u'ipaIDrange', u'ipadomainidrange'],
                    ipabaseid=[unicode(testrange1_base_id)],
                    ipabaserid=[unicode(testrange1_base_rid)],
                    ipasecondarybaserid=[unicode(testrange1_secondary_base_rid)],
                    ipaidrangesize=[unicode(testrange1_size)],
                    iparangetype=[u'local domain range'],
                ),
                value=testrange1,
                summary=u'Added ID range "%s"' % (testrange1),
            ),
        ),

        dict(
            desc='Retrieve ID range %r' % (testrange1),
            command=('idrange_show', [testrange1], dict()),
            expected=dict(
                result=dict(
                    dn=DN(('cn',testrange1),('cn','ranges'),('cn','etc'),
                          api.env.basedn),
                    cn=[testrange1],
                    ipabaseid=[unicode(testrange1_base_id)],
                    ipabaserid=[unicode(testrange1_base_rid)],
                    ipasecondarybaserid=[unicode(testrange1_secondary_base_rid)],
                    ipaidrangesize=[unicode(testrange1_size)],
                    iparangetype=[u'local domain range'],
                ),
                value=testrange1,
                summary=None,
            ),
        ),


        dict(
            desc='Create user %r in ID range %r' % (user1, testrange1),
            command=(
                'user_add', [user1], dict(givenname=u'Test', sn=u'User1',
                                          uidnumber=user1_uid)
            ),
            expected=dict(
                value=user1,
                summary=u'Added user "%s"' % user1,
                result=dict(
                    gecos=[u'Test User1'],
                    givenname=[u'Test'],
                    homedirectory=[u'/home/tuser1'],
                    krbprincipalname=[u'tuser1@' + api.env.realm],
                    loginshell=[u'/bin/sh'],
                    objectclass=objectclasses.user,
                    sn=[u'User1'],
                    uid=[user1],
                    uidnumber=[unicode(user1_uid)],
                    gidnumber=[unicode(user1_uid)],
                    displayname=[u'Test User1'],
                    cn=[u'Test User1'],
                    initials=[u'TU'],
                    mail=[u'%s@%s' % (user1, api.env.domain)],
                    ipauniqueid=[fuzzy_uuid],
                    mepmanagedentry=[DN(('cn',user1),('cn','groups'),('cn','accounts'),
                                        api.env.basedn)],
                    memberof_group=[u'ipausers'],
                    has_keytab=False,
                    has_password=False,
                    dn=DN(('uid',user1),('cn','users'),('cn','accounts'), api.env.basedn)
                ),
            ),
        ),


        dict(
            desc='Create group %r in ID range %r' % (group1, testrange1),
            command=(
                'group_add', [group1], dict(description=u'Test desc 1',
                                            gidnumber=group1_gid)
            ),
            expected=dict(
                value=group1,
                summary=u'Added group "%s"' % group1,
                result=dict(
                    cn=[group1],
                    description=[u'Test desc 1'],
                    gidnumber=[unicode(group1_gid)],
                    objectclass=objectclasses.group + [u'posixgroup'],
                    ipauniqueid=[fuzzy_uuid],
                    dn=DN(('cn',group1),('cn','groups'),('cn','accounts'), api.env.basedn),
                ),
            ),
        ),


        dict(
            desc='Try to modify ID range %r to get out bounds object #1' % (testrange1),
            command=(
                'idrange_mod', [testrange1], dict(ipabaseid=user1_uid + 1)
            ),
            expected=errors.ValidationError(name='ipabaseid,ipaidrangesize',
                error=u'range modification leaving objects with ID out of the'
                      u' defined range is not allowed'),
        ),


        dict(
            desc='Try to modify ID range %r to get out bounds object #2' % (testrange1),
            command=('idrange_mod', [testrange1], dict(ipaidrangesize=100)),
            expected=errors.ValidationError(name='ipabaseid,ipaidrangesize',
                error=u'range modification leaving objects with ID out of the'
                      u' defined range is not allowed'),
        ),


        dict(
            desc='Try to modify ID range %r to get out bounds object #3' % (testrange1),
            command=('idrange_mod', [testrange1], dict(ipabaseid=100, ipaidrangesize=100)),
            expected=errors.ValidationError(name='ipabaseid,ipaidrangesize',
                error=u'range modification leaving objects with ID out of the'
                      u' defined range is not allowed'),
        ),


        dict(
            desc='Modify ID range %r' % (testrange1),
            command=('idrange_mod', [testrange1], dict(ipaidrangesize=90000)),
            expected=dict(
                result=dict(
                    cn=[testrange1],
                    ipabaseid=[unicode(testrange1_base_id)],
                    ipabaserid=[unicode(testrange1_base_rid)],
                    ipasecondarybaserid=[unicode(testrange1_secondary_base_rid)],
                    ipaidrangesize=[u'90000'],
                    iparangetype=[u'local domain range'],
                ),
                value=testrange1,
                summary=u'Modified ID range "%s"' % (testrange1),
            ),
        ),


        dict(
            desc='Try to delete ID range %r with active IDs inside it' % testrange1,
            command=('idrange_del', [testrange1], {}),
            expected=errors.ValidationError(name='ipabaseid,ipaidrangesize',
                error=u'range modification leaving objects with ID out of the'
                      u' defined range is not allowed'),
        ),


        dict(
            desc='Delete user %r' % user1,
            command=('user_del', [user1], {}),
            expected=dict(
                result=dict(failed=u''),
                value=user1,
                summary=u'Deleted user "%s"' % user1,
            ),
        ),


        dict(
            desc='Delete group %r' % group1,
            command=('group_del', [group1], {}),
            expected=dict(
                result=dict(failed=u''),
                value=group1,
                summary=u'Deleted group "%s"' % group1,
            ),
        ),


        dict(
            desc='Delete ID range %r' % testrange1,
            command=('idrange_del', [testrange1], {}),
            expected=dict(
                result=dict(failed=u''),
                value=testrange1,
                summary=u'Deleted ID range "%s"' % testrange1,
            ),
        ),

        dict(
            desc='Create ID range %r' % (testrange2),
            command=('idrange_add', [testrange2],
                      dict(ipabaseid=testrange2_base_id,
                          ipaidrangesize=testrange2_size,
                          ipabaserid=testrange2_base_rid,
                          ipasecondarybaserid=testrange2_secondary_base_rid)),
            expected=dict(
                result=dict(
                    dn=DN(('cn',testrange2),('cn','ranges'),('cn','etc'),
                          api.env.basedn),
                    cn=[testrange2],
                    objectclass=[u'ipaIDrange', u'ipadomainidrange'],
                    ipabaseid=[unicode(testrange2_base_id)],
                    ipabaserid=[unicode(testrange2_base_rid)],
                    ipasecondarybaserid=[unicode(testrange2_secondary_base_rid)],
                    ipaidrangesize=[unicode(testrange2_size)],
                    iparangetype=[u'local domain range'],
                ),
                value=testrange2,
                summary=u'Added ID range "%s"' % (testrange2),
            ),
        ),

        dict(
            desc='Try to modify ID range %r so that its rid ranges are overlapping themselves' % (testrange2),
            command=('idrange_mod', [testrange2],
                      dict(ipabaserid=(testrange2_secondary_base_rid))),
            expected=errors.ValidationError(
                name='ID Range setup', error='Primary RID range and secondary RID range cannot overlap'),
        ),

        dict(
            desc='Try to create ID range %r with overlapping rid range' % (testrange3),
            command=('idrange_add', [testrange3],
                      dict(ipabaseid=testrange3_base_id,
                          ipaidrangesize=testrange3_size,
                          ipabaserid=testrange3_base_rid,
                          ipasecondarybaserid=testrange3_secondary_base_rid)),
            expected=errors.DatabaseError(
                desc='Constraint violation', info='New primary rid range overlaps with existing primary rid range.'),
        ),

       dict(
            desc='Try to create ID range %r with overlapping secondary rid range' % (testrange4),
            command=('idrange_add', [testrange4],
                      dict(ipabaseid=testrange4_base_id,
                          ipaidrangesize=testrange4_size,
                          ipabaserid=testrange4_base_rid,
                          ipasecondarybaserid=testrange4_secondary_base_rid)),
            expected=errors.DatabaseError(
                desc='Constraint violation', info='New secondary rid range overlaps with existing secondary rid range.'),
        ),

        dict(
            desc='Try to create ID range %r with primary range overlapping secondary rid range' % (testrange5),
            command=('idrange_add', [testrange5],
                      dict(ipabaseid=testrange5_base_id,
                          ipaidrangesize=testrange5_size,
                          ipabaserid=testrange5_base_rid,
                          ipasecondarybaserid=testrange5_secondary_base_rid)),
            expected=errors.DatabaseError(
                desc='Constraint violation', info='New primary rid range overlaps with existing secondary rid range.'),
        ),

        dict(
            desc='Try to create ID range %r with overlapping id range' % (testrange6),
            command=('idrange_add', [testrange6],
                      dict(ipabaseid=testrange6_base_id,
                          ipaidrangesize=testrange6_size,
                          ipabaserid=testrange6_base_rid,
                          ipasecondarybaserid=testrange6_secondary_base_rid)),
            expected=errors.DatabaseError(
                desc='Constraint violation', info='New base range overlaps with existing base range.'),
        ),

        dict(
            desc='Try to create ID range %r with rid ranges overlapping themselves' % (testrange7),
            command=('idrange_add', [testrange7],
                      dict(ipabaseid=testrange7_base_id,
                          ipaidrangesize=testrange7_size,
                          ipabaserid=testrange7_base_rid,
                          ipasecondarybaserid=testrange7_secondary_base_rid)),
            expected=errors.ValidationError(
                name='ID Range setup', error='Primary RID range and secondary RID range cannot overlap'),
        ),

        dict(
            desc='Delete ID range %r' % testrange2,
            command=('idrange_del', [testrange2], {}),
            expected=dict(
                result=dict(failed=u''),
                value=testrange2,
                summary=u'Deleted ID range "%s"' % testrange2,
            ),
        ),

        dict(
            desc='Create ID range %r' % (testrange8),
            command=('idrange_add', [testrange8],
                      dict(ipabaseid=testrange8_base_id,
                          ipaidrangesize=testrange8_size,
                          ipabaserid=testrange8_base_rid,
                          ipasecondarybaserid=testrange8_secondary_base_rid)),
            expected=dict(
                result=dict(
                    dn=DN(('cn',testrange8),('cn','ranges'),('cn','etc'),
                          api.env.basedn),
                    cn=[testrange8],
                    objectclass=[u'ipaIDrange', u'ipadomainidrange'],
                    ipabaseid=[unicode(testrange8_base_id)],
                    ipaidrangesize=[unicode(testrange8_size)],
                    iparangetype=[u'local domain range'],
                    ipabaserid=[unicode(testrange8_base_rid)],
                    ipasecondarybaserid=[unicode(testrange8_secondary_base_rid)]
                ),
                value=testrange8,
                summary=u'Added ID range "%s"' % (testrange8),
            ),
        ),

        dict(
            desc='Delete ID range %r' % testrange8,
            command=('idrange_del', [testrange8], {}),
            expected=dict(
                result=dict(failed=u''),
                value=testrange8,
                summary=u'Deleted ID range "%s"' % testrange8,
            ),
        ),

        dict(
            desc='Delete non-active AD trusted range %r' % testrange9,
            command=('idrange_del', [testrange9], {}),
            expected=dict(
                result=dict(failed=u''),
                value=testrange9,
                summary=u'Deleted ID range "%s"' % testrange9,
            ),
        ),

        dict(
            desc='Try to delete active AD trusted range %r' % testrange10,
            command=('idrange_del', [testrange10], {}),
            expected=errors.DependentEntry(
                    label='Active Trust',
                    key=testrange10,
                    dependent=testtrust),
        ),

    ]
