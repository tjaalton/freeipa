#
# Copyright (C) 2017  FreeIPA Contributors see COPYING for license
#

"""
Test the `session_storage.py` module.
"""

from ipapython import session_storage


class test_session_storage(object):
    """
    Test the session storage interface
    """

    def setup(self):
        # TODO: set up test user and kinit to it
        # tmpdir = tempfile.mkdtemp(prefix = "tmp-")
        # os.environ['KRB5CCNAME'] = 'FILE:%s/ccache' % tmpdir
        self.principal = 'admin'
        self.key = 'X-IPA-test-session-storage'
        self.data = 'Test Data'

    def test_01(self):
        session_storage.store_data(self.principal, self.key, self.data)

    def test_02(self):
        data = session_storage.get_data(self.principal, self.key)
        assert(data == self.data)

    def test_03(self):
        session_storage.remove_data(self.principal, self.key)
        try:
            session_storage.get_data(self.principal, self.key)
        except session_storage.KRB5Error:
            pass
