#
# Copyright (C) 2015  FreeIPA Contributors see COPYING for license
#

import re

import pytest

from ipatests.test_integration.base import IntegrationTest
from ipatests.test_integration import tasks
from ipatests.test_integration.env_config import get_global_config
from ipalib.constants import DOMAIN_SUFFIX_NAME
from ipatests.util import assert_deepequal

config = get_global_config()
reasoning = "Topology plugin disabled due to domain level 0"

@pytest.mark.skipif(config.domain_level == 0, reason=reasoning)
class TestTopologyOptions(IntegrationTest):
    num_replicas = 2
    topology = 'star'
    rawsegment_re = ('Segment name: (?P<name>.*?)',
                     '\s+Left node: (?P<lnode>.*?)',
                     '\s+Right node: (?P<rnode>.*?)',
                     '\s+Connectivity: (?P<connectivity>\S+)')
    segment_re = re.compile("\n".join(rawsegment_re))
    noentries_re = re.compile("Number of entries returned (\d+)")

    @classmethod
    def install(cls, mh):
        tasks.install_topo(cls.topology, cls.master,
                           cls.replicas[:-1],
                           cls.clients)

    def tokenize_topologies(self, command_output):
        """
        takes an output of `ipa topologysegment-find` and returns an array of
        segment hashes
        """
        segments = command_output.split("-----------------")[2]
        raw_segments = segments.split('\n\n')
        result = []
        for i in raw_segments:
            matched = self.segment_re.search(i)
            if matched:
                result.append({'leftnode': matched.group('lnode'),
                               'rightnode': matched.group('rnode'),
                               'name': matched.group('name'),
                               'connectivity': matched.group('connectivity')
                               }
                              )
        return result

    def test_topology_updated_on_replica_install_remove(self):
        """
        Install and remove a replica and make sure topology information is
        updated on all other replicas
        Testcase: http://www.freeipa.org/page/V4/Manage_replication_topology/
        Test_plan#Test_case:
        _Replication_topology_should_be_saved_in_the_LDAP_tree
        """
        tasks.kinit_admin(self.master)
        result1 = self.master.run_command(['ipa', 'topologysegment-find',
                                           DOMAIN_SUFFIX_NAME]).stdout_text
        first_segment_name = "%s-to-%s" % (self.master.hostname,
                                           self.replicas[0].hostname)
        expected_segment = {
            'connectivity': 'both',
            'leftnode': self.master.hostname,
            'name': first_segment_name,
            'rightnode': self.replicas[0].hostname}
        firstsegment = self.tokenize_topologies(result1)[0]
        assert_deepequal(expected_segment, firstsegment)
        tasks.install_replica(self.master, self.replicas[1], setup_ca=False,
                              setup_dns=False)
        # We need to make sure topology information is consistent across all
        # replicas
        result2 = self.master.run_command(['ipa', 'topologysegment-find',
                                           DOMAIN_SUFFIX_NAME])
        result3 = self.replicas[0].run_command(['ipa', 'topologysegment-find',
                                                DOMAIN_SUFFIX_NAME])
        result4 = self.replicas[1].run_command(['ipa', 'topologysegment-find',
                                                DOMAIN_SUFFIX_NAME])
        segments = self.tokenize_topologies(result2.stdout_text)
        assert(len(segments) == 2), "Unexpected number of segments found"
        assert_deepequal(result2.stdout_text, result3.stdout_text)
        assert_deepequal(result3.stdout_text,  result4.stdout_text)
        # Now let's check that uninstalling the replica will update the topology
        # info on the rest of replicas.
        tasks.uninstall_master(self.replicas[1])
        tasks.clean_replication_agreement(self.master, self.replicas[1])
        result5 = self.master.run_command(['ipa', 'topologysegment-find',
                                           DOMAIN_SUFFIX_NAME])
        num_entries = self.noentries_re.search(result5.stdout_text).group(1)
        assert(num_entries == "1"), "Incorrect number of entries displayed"

    def test_add_remove_segment(self):
        """
        Make sure a topology segment can be manually created and deleted
        with the influence on the real topology
        Testcase http://www.freeipa.org/page/V4/Manage_replication_topology/
        Test_plan#Test_case:_Basic_CRUD_test
        """
        tasks.kinit_admin(self.master)
        # Install the second replica
        tasks.install_replica(self.master, self.replicas[1], setup_ca=False,
                              setup_dns=False)
        # turn a star into a ring
        segment, err = tasks.create_segment(self.master,
                                            self.replicas[0],
                                            self.replicas[1])
        assert err == "", err
        # Make sure the new segment is shown by `ipa topologysegment-find`
        result1 = self.master.run_command(['ipa', 'topologysegment-find',
                                           DOMAIN_SUFFIX_NAME]).stdout_text
        assert(segment['name'] in result1), (
            "%s: segment not found" % segment['name'])
        # Remove master <-> replica2 segment and make sure that the changes get
        # there through replica1
        deleteme = "%s-to-%s" % (self.master.hostname,
                                 self.replicas[1].hostname)
        returncode, error = tasks.destroy_segment(self.master, deleteme)
        assert returncode == 0, error
        # Wait till replication ends and make sure replica1 does not have
        # segment that was deleted on master
        replica1_ldap = self.replicas[0].ldap_connect()
        tasks.wait_for_replication(replica1_ldap)
        result3 = self.replicas[0].run_command(['ipa', 'topologysegment-find',
                                               DOMAIN_SUFFIX_NAME]).stdout_text
        assert(deleteme not in result3), "%s: segment still exists" % deleteme
        # Create test data on master and make sure it gets all the way down to
        # replica2 through replica1
        self.master.run_command(['ipa', 'user-add', 'someuser',
                                 '--first', 'test',
                                 '--last', 'user'])
        dest_ldap = self.replicas[1].ldap_connect()
        tasks.wait_for_replication(dest_ldap)
        result4 = self.replicas[1].run_command(['ipa', 'user-find'])
        assert('someuser' in result4.stdout_text), 'User not found: someuser'
        # We end up having a line topology: master <-> replica1 <-> replica2

    def test_remove_the_only_connection(self):
        """
        Testcase: http://www.freeipa.org/page/V4/Manage_replication_topology/
        Test_plan#Test_case:
        _Removal_of_a_topology_segment_is_allowed_only_if_there_is_at_least_one_more_segment_connecting_the_given_replica
        """
        text = "Removal of Segment disconnects topology"
        error1 = "The system should not have let you remove the segment"
        error2 = "Wrong error message thrown during segment removal: \"%s\""
        replicas = (self.replicas[0].hostname, self.replicas[1].hostname)

        returncode, error = tasks.destroy_segment(self.master, "%s-to-%s" % replicas)
        assert returncode != 0, error1
        assert error.count(text) == 1, error2 % error
        newseg, err = tasks.create_segment(self.master,
                                           self.master,
                                           self.replicas[1])
        assert err == "", err
        returncode, error = tasks.destroy_segment(self.master, "%s-to-%s" % replicas)
        assert returncode == 0, error
