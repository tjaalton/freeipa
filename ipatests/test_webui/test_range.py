# Authors:
#   Petr Vobornik <pvoborni@redhat.com>
#
# Copyright (C) 2013  Red Hat
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
Range tests
"""

import pytest

import ipatests.test_webui.test_trust as trust_mod
from ipatests.test_webui.ui_driver import screenshot
from ipatests.test_webui.task_range import (
    range_tasks,
    LOCAL_ID_RANGE,
    TRUSTED_ID_RANGE,
)

ENTITY = 'idrange'
PKEY = 'itest-range'


@pytest.mark.tier1
class test_range(range_tasks):

    @pytest.fixture(autouse=True)
    def range_setup(self, ui_driver_fsetup):
        self.init_app()
        self.get_shifts()

        self.range_types = [LOCAL_ID_RANGE]
        if self.has_trusts():
            self.range_types.append(TRUSTED_ID_RANGE)

    @screenshot
    def test_crud(self):
        """
        Basic CRUD: range
        """
        self.basic_crud(ENTITY, self.get_data(PKEY), mod=False)

    @screenshot
    def test_mod(self):
        """
        Test mod operating in a new range
        """
        data = self.get_data(PKEY)

        self.add_record(ENTITY, data)
        self.navigate_to_record(PKEY)

        # changes idrange and tries to save it
        self.fill_fields(data['mod'], undo=True)
        self.assert_facet_button_enabled('save')
        self.facet_button_click('save')
        self.wait_for_request(n=2)

        # the user should not be able to change the ID allocation for
        # IPA domain, as it's explained in https://pagure.io/freeipa/issue/4826
        dialog = self.get_last_error_dialog()
        assert ("can not be used to change ID allocation for local IPA domain"
                in dialog.text)
        self.dialog_button_click('cancel')
        self.navigate_to_entity(ENTITY)
        self.wait_for_request()
        self.delete_record(PKEY)

    @screenshot
    def test_types(self):
        """
        Test range types

        Only 'local' and 'ipa-ad-trust' types are tested since range validation
        made quite hard to test the other types:

        - 'ipa-ad-trust-posix' can be tested only with subdomains.
        - 'ipa-ad-winsync' and 'ipa-ipa-trust' and  are not supported yet
          https://fedorahosted.org/freeipa/ticket/4323
        """

        pkey_local = 'itest-local'
        pkey_ad = 'itest-ad'
        column = 'iparangetype'

        data = self.get_data(pkey_local)
        self.add_record(ENTITY, data)
        self.assert_record_value('local domain range', pkey_local, column)

        if self.has_trusts():

            trust_tasks = trust_mod.trust_tasks()
            trust_data = trust_tasks.get_data()

            self.add_record(trust_mod.ENTITY, trust_data)

            domain = self.get_domain()

            self.navigate_to_entity(ENTITY)

            data = self.get_data(pkey_ad, range_type=TRUSTED_ID_RANGE,
                                 domain=domain)
            self.add_record(ENTITY, data, navigate=False)
            self.assert_record_value('Active Directory domain range', pkey_ad,
                                     column)

            self.delete(trust_mod.ENTITY, [trust_data])
            self.navigate_to_entity(ENTITY)
            self.delete_record(pkey_ad)

        self.delete_record(pkey_local)

    @screenshot
    def test_add_range_with_special_characters_in_name(self):
        """
        Test creating ID Range with special characters in name
        """
        data = self.get_data('itest-range-!@#$%^&*')
        self.add_record(ENTITY, data, delete=True)

    @screenshot
    def test_add_range_with_existing_name(self):
        """
        Test creating ID Range with existing range name
        """
        for range_type in self.range_types:
            pkey = 'itest-range-{}'.format(range_type)
            data = self.get_data(pkey, range_type=range_type)

            self.add_record(ENTITY, data)
            self.add_record(ENTITY, data, navigate=False, negative=True,
                            pre_delete=False)

            dialog = self.get_last_error_dialog()

            try:
                assert ('range with name "{}" already exists'.format(pkey)
                        in dialog.text)
            finally:
                self.delete_record(pkey)

    @screenshot
    def test_add_range_with_existing_base_id(self):
        """
        Test creating ID Range with existing base ID
        """
        for range_type in self.range_types:
            pkey = 'itest-range-original'
            form_data = self.get_add_form_data(pkey)
            data = self.get_data(pkey, form_data=form_data)
            form_data.range_type = range_type
            duplicated_data = self.get_data(form_data=form_data)

            self.add_record(ENTITY, data)
            self.add_record(ENTITY, duplicated_data, navigate=False,
                            negative=True, pre_delete=False)

            dialog = self.get_last_error_dialog()

            try:
                assert self.BASE_RANGE_OVERLAPS_ERROR in dialog.text
            finally:
                self.delete_record(pkey)

    @screenshot
    def test_add_range_overlaps_with_existing(self):
        """
        Test creating ID Range with overlapping of existing range
        """
        for range_type in self.range_types:
            pkey = 'itest-range'
            pkey_overlaps = 'itest-range-overlaps'

            form_data = self.get_add_form_data(pkey)
            data = self.get_data(pkey, form_data=form_data)
            form_data_overlaps = self.get_add_form_data(
                pkey_overlaps,
                base_id=form_data.base_id + form_data.size - 1,
                range_type=range_type
            )
            data_overlaps = self.get_data(form_data=form_data_overlaps)

            self.add_record(ENTITY, data)
            self.add_record(ENTITY, data_overlaps, navigate=False,
                            negative=True, pre_delete=False)

            dialog = self.get_last_error_dialog()

            try:
                assert self.BASE_RANGE_OVERLAPS_ERROR in dialog.text
            finally:
                self.delete_record(pkey)

    @screenshot
    def test_add_range_with_overlapping_primary_and_secondary_rid(self):
        """
        Test creating ID Range with overlapping of primary and secondary RID
        """
        form_data = self.get_add_form_data(PKEY)
        form_data.secondary_base_rid = form_data.base_rid
        data = self.get_data(PKEY, form_data=form_data)

        self.add_record(ENTITY, data, negative=True)
        dialog = self.get_last_error_dialog()

        try:
            assert self.PRIMARY_AND_SECONDARY_RID_OVERLAP_ERROR in dialog.text
        finally:
            self.delete_record(PKEY)

    @screenshot
    def test_add_range_with_existing_base_rid(self):
        """
        Test creating ID Range with existing primary RID base
        """
        form_data = self.get_add_form_data(PKEY)
        data = self.get_data(PKEY, form_data=form_data)

        # Get RID base from previous form
        duplicated_data = self.get_data(base_rid=form_data.base_rid)

        self.add_record(ENTITY, data)
        self.add_record(ENTITY, duplicated_data, navigate=False, negative=True,
                        pre_delete=False)

        dialog = self.get_last_error_dialog()

        try:
            assert self.PRIMARY_RID_RANGE_OVERLAPS_ERROR in dialog.text
        finally:
            self.delete_record(PKEY)

    @screenshot
    def test_add_range_with_existing_secondary_rid(self):
        """
        Test creating ID Range with existing secondary RID base
        """
        form_data = self.get_add_form_data(PKEY)
        data = self.get_data(PKEY, form_data=form_data)
        # Get RID base from previous form
        duplicated_data = self.get_data(
            secondary_base_rid=form_data.secondary_base_rid
        )

        self.add_record(ENTITY, data)
        self.add_record(ENTITY, duplicated_data, navigate=False, negative=True,
                        pre_delete=False)

        dialog = self.get_last_error_dialog()

        try:
            assert self.SECONDARY_RID_RANGE_OVERLAPS_ERROR in dialog.text
        finally:
            self.delete_record(PKEY)

    @screenshot
    def test_add_range_without_rid(self):
        """
        Test creating ID Range without giving rid-base or/and
        secondary-rid-base values
        """
        pkey = 'itest-range-without-rid'

        # Without primary RID base
        data = self.get_data(pkey, base_rid='')
        self.add_record(ENTITY, data, negative=True)
        try:
            assert self.has_form_error('ipabaserid')
        finally:
            self.delete_record(pkey)

        self.dialog_button_click('cancel')

        # Without secondary RID base
        data = self.get_data(pkey, secondary_base_rid='')
        self.add_record(ENTITY, data, navigate=False, negative=True)
        try:
            assert self.has_form_error('ipasecondarybaserid')
        finally:
            self.delete_record(pkey)

        self.dialog_button_click('cancel')

        # Without primary and secondary RID bases
        data = self.get_data(pkey, base_rid='', secondary_base_rid='')
        self.add_record(ENTITY, data, navigate=False)
        self.delete_record(pkey)

    @screenshot
    def test_modify_range_with_invalid_or_missing_values(self):
        """
        Test modification ID range with empty values of options
        """
        cases = [
            # Empty values
            {
                'base_id': '',
                'base_rid': '',
                'secondary_base_rid': '',
                'size': '',
            },
            # Out of range
            {'base_id': 2 ** 32},
            {'size': 2 ** 32},
            {'base_rid': 2 ** 32},
            {'secondary_base_rid': 2 ** 32},
            # Invalid value
            {'base_id': 1.1},
            {'size': 1.1},
            {'base_rid': 1.1},
            {'secondary_base_rid': 1.1},
        ]
        data = self.get_data(PKEY)

        self.add_record(ENTITY, data)
        self.navigate_to_record(PKEY)

        for values in cases:
            form_data = self.get_mod_form_data(**values)

            self.fill_fields(form_data.serialize(), undo=True)
            self.assert_facet_button_enabled('save')
            self.facet_button_click('save')

            self.assert_notification(
                type='danger',
                assert_text='Input form contains invalid or missing values.'
            )
            self.close_notifications()
            self.facet_button_click('revert')

        self.delete_record(PKEY)

    @screenshot
    def test_delete_primary_local_range(self):
        """
        Test deleting primary local ID range
        """
        ipa_realm = self.config.get('ipa_realm')
        pkey = '{}_id_range'.format(ipa_realm)

        self.navigate_to_entity(ENTITY)
        self.delete_record(pkey)

        self.assert_last_error_dialog(
            self.DELETE_PRIMARY_LOCAL_RANGE_ERROR,
            details=True
        )
