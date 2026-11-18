import copy
import json
from pathlib import Path
import tempfile
import unittest

from operator_apply import plan_packages, resume_state, verify_recovery, rebase_owned, recover_cached_archive
from types import SimpleNamespace
from maintenance import Maintenance
from test_update_guard import healthy
from update_guard import cluster_baseline


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.data = healthy()
        self.nodes = list(self.data)
        self.boots = {n:d['boot_id'] for n,d in self.data.items()}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'state.json'
        self.state = {'node':'NODE-1','phase':'prepared','baseline':cluster_baseline(self.data),
                      'original_crm_active':'active','original_crm_unit':'enabled',
                      'original_maintenance':False,'maintenance_owned':True,
                      'mask_owned':True,'crm_stop_owned':True}
        self.path.write_text(json.dumps(self.state))

    def test_election_change_accepted_only_after_complete_recovery(self):
        for row in self.data.values():row['ceph']['election_epoch'] += 4
        baseline = verify_recovery(self.nodes, self.data, self.boots)
        self.assertEqual(baseline['NODE-1']['ceph_election_epoch'],104)
        self.data['NODE-2']['ceph']['election_epoch'] -= 1
        with self.assertRaisesRegex(ValueError,'disagree'):
            verify_recovery(self.nodes, self.data, self.boots)

    def test_force_never_accepts_reboot_missing_osd_or_bad_health_at_boundary(self):
        for mutation in [lambda d:d['NODE-1'].update(boot_id='changed'),
                         lambda d:d['NODE-1']['ceph']['osdmap'].update(num_up_osds=3),
                         lambda d:d['NODE-1']['ceph']['health'].update(status='HEALTH_WARN')]:
            data=copy.deepcopy(self.data);mutation(data)
            with self.assertRaises(ValueError):verify_recovery(self.nodes,data,self.boots)

    def test_only_reviewed_partial_package_is_allowed_before_repair(self):
        self.data['NODE-1']['audit']='They must be reinstalled:\n libpve-notify-perl Notify helper module.'
        verify_recovery(self.nodes,self.data,self.boots,('NODE-1','libpve-notify-perl'))
        self.assertTrue(self.data['NODE-1']['audit'])
        with self.assertRaises(ValueError):verify_recovery(self.nodes,self.data,self.boots)
        self.data['NODE-1']['audit']+='\n pve-manager Another package.'
        with self.assertRaisesRegex(ValueError,'Unreviewed'):
            verify_recovery(self.nodes,self.data,self.boots,('NODE-1','libpve-notify-perl'))

    def test_unreviewed_peer_package_damage_blocks_recovery(self):
        self.data['NODE-2']['audit']='broken peer package'
        with self.assertRaises(ValueError):
            verify_recovery(self.nodes,self.data,self.boots,('NODE-1','libpve-notify-perl'))

    def test_owned_preparation_can_resume(self):
        self.assertEqual(resume_state(self.path,'NODE-1',self.data,self.boots),self.state)

    def test_no_resume_if_watchdog_active_guests_present_or_other_node_held(self):
        for mutation in [lambda d:d['NODE-1'].update(watchdog_active=True),
                         lambda d:d['NODE-1']['resources'].append({'type':'qemu','node':'NODE-1','status':'running'}),
                         lambda d:d['NODE-2']['manager']['node_request'].update({'NODE-2':{'maintenance':1}})]:
            data=copy.deepcopy(self.data);mutation(data)
            with self.assertRaises(ValueError):resume_state(self.path,'NODE-1',data,self.boots)

    def test_no_resume_of_unowned_or_changed_preparation(self):
        for key,value in [('mask_owned',False),('original_maintenance',True),
                          ('phase','restored'),('original_crm_unit','disabled')]:
            state=copy.deepcopy(self.state);state[key]=value
            self.path.write_text(json.dumps(state))
            with self.assertRaises(ValueError):resume_state(self.path,'NODE-1',self.data,self.boots)

    def test_rebase_preserves_previous_evidence(self):
        helper=Maintenance(self.nodes,'NODE-1',self.path,self.state['baseline'])
        for row in self.data.values():row['ceph']['election_epoch']=104
        baseline=verify_recovery(self.nodes,self.data,self.boots)
        rebase_owned(helper,self.data,baseline)
        state=json.loads(self.path.read_text())
        self.assertEqual(state['operator_previous_baselines'],[self.state['baseline']])
        self.assertEqual(state['baseline'],baseline)

    def test_removal_and_kept_back_packages_rejected(self):
        for output in ['Remv pve-manager [9.2.30]','0 upgraded, 1 not upgraded.']:
            with self.assertRaises(ValueError):plan_packages(output)
        self.assertEqual(plan_packages('Inst pve-manager [9.2.29] (9.2.30 x)\n0 not upgraded.'),['pve-manager'])
        self.assertEqual(plan_packages('0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.'),[])

    def test_scoped_repair_leaves_other_updates_for_the_full_upgrade(self):
        output='Inst libpve-notify-perl [9.1.9] (9.1.11 x)\n4 upgraded, 0 newly installed, 0 to remove and 14 not upgraded.'
        self.assertEqual(plan_packages(output, complete=False),['libpve-notify-perl'])
        with self.assertRaises(ValueError):plan_packages(output)
        with self.assertRaises(ValueError):plan_packages('Remv pve-manager [9.2.30]',complete=False)

    def test_cached_archive_replays_unpack_before_apt(self):
        archive=Path(self.tmp.name)/'libpve-notify-perl_9.1.10_all.deb';archive.touch()
        calls=[]
        def query(args,**kw):
            if args[0]=='dpkg-query':return '9.1.9'
            return 'libpve-notify-perl' if args[-1]=='Package' else '9.1.10'
        def execute(args,**kw):calls.append((args,kw));return SimpleNamespace(returncode=0)
        recover_cached_archive('libpve-notify-perl',Path(self.tmp.name),query,execute)
        self.assertEqual(calls[-1],(['dpkg','--unpack',str(archive)],{'check':True}))

    def test_missing_wrong_or_older_cached_archive_never_unpacks(self):
        for case in ['missing','wrong','older']:
            with tempfile.TemporaryDirectory() as tmp:
                if case!='missing':(Path(tmp)/'libpve-notify-perl_1_all.deb').touch()
                calls=[]
                def query(args,**kw):
                    if args[0]=='dpkg-query':return '9.1.9'
                    return 'wrong-package' if case=='wrong' else ('libpve-notify-perl' if args[-1]=='Package' else '1')
                def execute(args,**kw):calls.append(args);return SimpleNamespace(returncode=1)
                with self.assertRaises(ValueError):recover_cached_archive('libpve-notify-perl',Path(tmp),query,execute)
                self.assertFalse(any('--unpack' in a for a in calls))


if __name__=='__main__':unittest.main()
