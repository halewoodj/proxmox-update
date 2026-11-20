"""Safety decisions using cluster fixtures; never contacts a host."""
import copy
from pathlib import Path
import pathlib
import tempfile
import unittest
from update_guard import WATCHDOG_PROBE, cluster_baseline, verify_cluster, verify_target, verify_selection, snapshot, read_verified, ClusterPending, collect_snapshots


def healthy():
    result = {}
    for n in ['NODE-1','NODE-2']:
        result[n] = {
            'hostname':n, 'boot_id':'a'*8+'-'+ 'b'*4+'-'+ 'c'*4+'-'+ 'd'*4+'-'+'e'*12,
            'members':'1 1 NODE-1\n2 1 NODE-2', 'quorum':'Quorate: Yes',
            'ceph':{'health':{'status':'HEALTH_OK','mutes':[]},'election_epoch':100,
                    'osdmap':{'num_osds':4,'num_up_osds':4,'num_in_osds':4},
                    'pgmap':{'num_pgs':64,'pgs_by_state':[{'state_name':'active+clean','count':64}]}},
            'audit':'', 'now':1000,
            'ha_status':[{'type':'master','node':'NODE-2','timestamp':995},
                         {'type':'lrm','node':'NODE-1','timestamp':995},
                         {'type':'lrm','node':'NODE-2','timestamp':995}],
            'ha':'quorum OK\nmaster NODE-2 (active, now)\nfencing armed (CRM watchdog active)\n'
                 'lrm NODE-1 (maintenance, watchdog standby, now)\n'
                 'lrm NODE-2 (active, watchdog active, now)\nservice vm:100 (NODE-2, started)',
            'resources':[{'type':'qemu','node':'NODE-2','status':'running'}],
            'crm_active':'inactive','crm_unit':'masked-runtime',
            'manager':{'node_request':{'NODE-1':{'maintenance':1}}},'crm_commands':'',
            'watchdog_active':False,'watchdog_verified':True,
        }
    return result


class GuardTests(unittest.TestCase):
    def test_existing_maintenance_blocks_conflicting_selection_upfront(self):
        data=healthy()
        for selected in [['NODE-1','NODE-2'],['NODE-2']]:
            with self.subTest(selected=selected):
                with self.assertRaisesRegex(ValueError,'existing HA maintenance on NODE-1'):
                    verify_selection(list(data),selected,data)
        verify_selection(list(data),['NODE-1'],data)

    def test_selection_checks_lrm_holds_and_all_observers(self):
        data=healthy()
        for d in data.values():d['manager']={}
        with self.assertRaisesRegex(ValueError,'existing HA maintenance'):
            verify_selection(list(data),list(data),data)
        for d in data.values():d['ha']=d['ha'].replace('maintenance, watchdog standby','active, watchdog active')
        verify_selection(list(data),list(data),data)
        data['NODE-2']['manager']={'node_request':{'NODE-1':{'maintenance':1}}}
        with self.assertRaisesRegex(ValueError,'existing HA maintenance'):
            verify_selection(list(data),list(data),data)

    def test_invalid_selection_is_rejected(self):
        data=healthy()
        for selected in [[],['NODE-1','NODE-1'],['NODE-3']]:
            with self.assertRaises(ValueError):verify_selection(list(data),selected,data)

    def test_ready_cluster_and_evacuated_target(self):
        data=healthy()
        verify_cluster(list(data),data)
        verify_target('NODE-1',data)

    def test_installed_proxmox_label_requires_actual_released_connections(self):
        for active in [False,True]:
            with self.subTest(active=active):
                data=healthy()
                for d in data.values():
                    d['ha']=d['ha'].replace('maintenance, watchdog standby',
                                          'maintenance mode, watchdog active')
                data['NODE-1']['watchdog_active']=active
                if active:
                    with self.assertRaisesRegex(ValueError,'active clients'):
                        verify_target('NODE-1',data)
                else:
                    verify_target('NODE-1',data)

    def test_missing_watchdog_connection_evidence_is_rejected(self):
        data=healthy();data['NODE-1'].pop('watchdog_verified')
        with self.assertRaisesRegex(ValueError,'evidence is missing'):verify_target('NODE-1',data)

    def test_watchdog_socket_probe_detects_clients_and_fails_closed(self):
        namespace={'pathlib':pathlib};exec(WATCHDOG_PROBE,namespace)
        probe=namespace['watchdog_probe']
        with tempfile.TemporaryDirectory() as tmp:
            proc=Path(tmp)/'proc';run=Path(tmp)/'run'
            (proc/'42/fd').mkdir(parents=True);(proc/'net').mkdir();run.mkdir()
            (proc/'42/comm').write_text('watchdog-mux\n')
            (proc/'42/fd/4').symlink_to('socket:[100]')
            listener='0: 00000002 00000000 00010000 0001 01 100 '+str(run/'watchdog-mux.sock')+'\n'
            client='0: 00000003 00000000 00000000 0001 03 101 '+str(run/'watchdog-mux.sock')+'\n'
            (proc/'net/unix').write_text('header\n'+listener)
            self.assertEqual(probe(proc,run,42),{'watchdog_active':False,'watchdog_verified':True})
            (run/'watchdog-mux.active').mkdir()
            self.assertTrue(probe(proc,run,42)['watchdog_active'])
            (run/'watchdog-mux.active').rmdir()
            (proc/'42/fd/6').symlink_to('socket:[101]')
            (proc/'net/unix').write_text('header\n'+listener+client)
            self.assertTrue(probe(proc,run,42)['watchdog_active'])
            (proc/'42/fd/6').unlink()
            with self.assertRaisesRegex(RuntimeError,'ownership'):probe(proc,run,42)
            (proc/'net/unix').write_text('header\n')
            with self.assertRaises(RuntimeError):probe(proc,run,42)

    def test_each_unhealthy_boundary_is_rejected(self):
        cases=[
            ('missing node',lambda d:d.pop('NODE-2')),
            ('lost quorum',lambda d:d['NODE-1'].update(quorum='Quorate: No')),
            ('lost member',lambda d:d['NODE-1'].update(members='1 1 NODE-1')),
            ('dirty dpkg',lambda d:d['NODE-1'].update(audit='kernel half configured')),
            ('ceph warn',lambda d:d['NODE-1']['ceph']['health'].update(status='HEALTH_WARN')),
            ('muted warning',lambda d:d['NODE-1']['ceph']['health'].update(mutes=['OSD_DOWN'])),
            ('missing OSD',lambda d:d['NODE-1']['ceph']['osdmap'].update(num_up_osds=3)),
            ('degraded PG',lambda d:d['NODE-1']['ceph']['pgmap']['pgs_by_state'][0].update(state_name='active+degraded')),
            ('stale HA',lambda d:d['NODE-1']['ha_status'][0].update(timestamp=900)),
        ]
        for label,change in cases:
            with self.subTest(label=label):
                data=healthy();change(data)
                with self.assertRaises(ValueError):verify_cluster(['NODE-1','NODE-2'],data)

    def test_unexpected_reboot_is_rejected_even_after_health_recovers(self):
        data=healthy(); baseline=cluster_baseline(data)
        data['NODE-1']['boot_id']='f'*8+'-'+ 'b'*4+'-'+ 'c'*4+'-'+ 'd'*4+'-'+'e'*12
        with self.assertRaisesRegex(ValueError,'Unexpected reboot'):
            verify_cluster(list(data),data,baseline)

    def test_recovered_election_is_allowed_but_requires_two_stable_reads(self):
        data=healthy();baseline=cluster_baseline(data)
        for d in data.values():d['ceph']['election_epoch']+=2
        self.assertTrue(verify_cluster(list(data),data,baseline))
        reads=[];sleeps=[]
        def collect():reads.append(True);return copy.deepcopy(data)
        read_verified(list(data),baseline,collect=collect,sleep=sleeps.append)
        self.assertEqual(len(reads),2);self.assertEqual(sleeps,[5])
        reads.clear()
        read_verified(list(data),baseline,collect=collect,confirmed_epoch=102,sleep=sleeps.append)
        self.assertEqual(len(reads),1)

    def test_changing_election_must_settle_and_regression_is_rejected(self):
        data=healthy();baseline=cluster_baseline(data);reads=[];sleeps=[]
        def collect():
            value=copy.deepcopy(data);epoch=[102,104,104][len(reads)];reads.append(epoch)
            for d in value.values():d['ceph']['election_epoch']=epoch
            return value
        read_verified(list(data),baseline,collect=collect,sleep=sleeps.append)
        self.assertEqual(reads,[102,104,104])
        for d in data.values():d['ceph']['election_epoch']=98
        with self.assertRaisesRegex(ValueError,'regressed'):verify_cluster(list(data),data,baseline)

    def test_snapshots_crossing_an_election_are_read_again(self):
        base=healthy();baseline=cluster_baseline(base);reads=[]
        def collect():
            value=copy.deepcopy(base);reads.append(True)
            for row in value.values():row['ceph']['election_epoch']=102
            if len(reads)==1:value['NODE-1']['ceph']['election_epoch']=100
            return value
        read_verified(list(base),baseline,collect=collect,sleep=lambda _:None)
        self.assertEqual(len(reads),3)
        bad=collect();bad['NODE-1']['ceph']['election_epoch']=104;bad['NODE-2']['quorum']='Quorate: No'
        with self.assertRaisesRegex(ValueError,'No quorum'):verify_cluster(list(base),bad,baseline)

    def test_transient_package_lock_waits_without_disabling_real_checks(self):
        calls=[];now=[0]
        def read(node):
            calls.append(node)
            if len(calls)<3:raise ValueError('fcntl.lockf: BlockingIOError: Resource temporarily unavailable')
            return node,healthy()[node]
        def sleep(seconds):now[0]+=seconds
        node,data=snapshot('NODE-1',read=read,sleep=sleep,clock=lambda:now[0])
        self.assertEqual(node,'NODE-1');self.assertEqual(len(calls),3);self.assertEqual(now[0],10)
        verify_cluster(['NODE-1','NODE-2'],healthy())

    def test_long_lock_wait_discards_other_nodes_earlier_snapshots(self):
        now=[0];calls={'NODE-1':0,'NODE-2':0}
        def read(node):
            calls[node]+=1
            if node=='NODE-2' and calls[node]==1:now[0]+=60
            data=healthy()[node];data['sample_generation']=calls[node]
            return node,data
        data=collect_snapshots(list(calls),read=read,clock=lambda:now[0])
        self.assertTrue(all(row['sample_generation']==2 for row in data.values()))
        def slow(node):now[0]+=40;return node,healthy()[node]
        with self.assertRaisesRegex(ValueError,'fresh cluster observations'):
            collect_snapshots(list(calls),read=slow,clock=lambda:now[0],timeout=60)

    def test_persistent_lock_and_read_timeouts_are_bounded(self):
        now=[0]
        def sleep(seconds):now[0]+=seconds
        def locked(node):raise ValueError('fcntl.lockf: BlockingIOError: Resource temporarily unavailable')
        with self.assertRaisesRegex(ValueError,'Timed out waiting'):
            snapshot('NODE-1',read=locked,sleep=sleep,clock=lambda:now[0],lock_timeout=6)
        calls=[]
        def timed_out(node):calls.append(node);raise ValueError('ceph status timed out')
        with self.assertRaises(ValueError):snapshot('NODE-1',read=timed_out,sleep=lambda _:None)
        self.assertEqual(len(calls),3)
        def bad_identity(node):raise ValueError('unexpected identity')
        with self.assertRaisesRegex(ValueError,'identity'):
            snapshot('NODE-1',read=bad_identity,sleep=lambda _:self.fail('Must not retry'))

    def test_known_ceph_warning_waits_for_recovery_but_not_for_unsafe_osds(self):
        data=healthy();baseline=cluster_baseline(data);reads=[]
        def collect():
            value=copy.deepcopy(data);reads.append(True)
            if len(reads)==1:
                for d in value.values():
                    d['ceph'].update(monmap={'num_mons':2},quorum_names=['NODE-1','NODE-2'])
                    d['ceph']['health'].update(status='HEALTH_WARN',checks={'SLOW_OPS':{}})
            return value
        read_verified(list(data),baseline,collect=collect,sleep=lambda _:None)
        self.assertEqual(len(reads),3)
        for d in data.values():
            d['ceph'].update(monmap={'num_mons':2},quorum_names=['NODE-1','NODE-2'])
            d['ceph']['health'].update(status='HEALTH_WARN',checks={'SLOW_OPS':{}})
        with self.assertRaises(ClusterPending):verify_cluster(list(data),data,baseline)
        data['NODE-1']['ceph']['osdmap']['num_up_osds']=3
        with self.assertRaisesRegex(ValueError,'missing OSD'):verify_cluster(list(data),data,baseline)

    def test_persistent_ceph_warning_times_out_without_permission_to_proceed(self):
        data=healthy();now=[0]
        for d in data.values():
            d['ceph'].update(monmap={'num_mons':2},quorum_names=['NODE-1','NODE-2'])
            d['ceph']['health'].update(status='HEALTH_WARN',checks={'SLOW_OPS':{}})
        def sleep(seconds):now[0]+=seconds
        with self.assertRaisesRegex(ValueError,'Timed out waiting'):
            read_verified(list(data),collect=lambda:copy.deepcopy(data),timeout=6,sleep=sleep,clock=lambda:now[0])

    def test_disagreeing_or_invalid_election_evidence_is_rejected(self):
        for value in [101,0,'100']:
            data=healthy();data['NODE-1']['ceph']['election_epoch']=value
            with self.assertRaisesRegex(ValueError,'election evidence'):
                verify_cluster(list(data),data)

    def test_clean_background_scrub_is_allowed(self):
        for state in ['active+clean+scrubbing','active+clean+scrubbing+deep']:
            data=healthy();data['NODE-1']['ceph']['pgmap']['pgs_by_state'][0]['state_name']=state
            verify_cluster(list(data),data)

    def test_backup_snapshot_cleanup_remains_active_and_clean(self):
        # Task 25814's failure window: all 577 PGs stayed active+clean,
        # including backup snapshot trimming and normal deep scrubbing.
        data=healthy()
        for d in data.values():
            d['ceph']['pgmap']={'num_pgs':577,'pgs_by_state':[
                {'state_name':'active+clean+snaptrim','count':22},
                {'state_name':'active+clean+snaptrim_wait','count':115},
                {'state_name':'active+clean+scrubbing+deep','count':11},
                {'state_name':'active+clean','count':429}]}
        verify_cluster(list(data),data)
        for state in ['active+clean+snaptrim+scrubbing+deep','active+clean+snaptrim_wait']:
            value=healthy();value['NODE-1']['ceph']['pgmap']['pgs_by_state'][0]['state_name']=state
            verify_cluster(list(value),value)

    def test_cleanup_flags_never_hide_missing_replicas_errors_or_unknown_states(self):
        for state in ['active+snaptrim','clean+snaptrim','active+clean+snaptrim_error',
                      'active+clean+laggy','active+clean+degraded+snaptrim',
                      'active+clean+recovering','active+clean+unexpected']:
            with self.subTest(state=state):
                data=healthy();data['NODE-1']['ceph']['pgmap']['pgs_by_state'][0]['state_name']=state
                with self.assertRaisesRegex(ValueError,state.replace('+',r'\+')):
                    verify_cluster(list(data),data)
        for state in ['active+clean+remapped','active+clean+repair','active+clean+unknown']:
            data=healthy();data['NODE-1']['ceph']['pgmap']['pgs_by_state'][0]['state_name']=state
            with self.assertRaises(ValueError):verify_cluster(list(data),data)

    def test_local_watchdog_client_blocks_even_with_maintenance_label(self):
        data=healthy();data['NODE-1']['watchdog_active']=True
        with self.assertRaisesRegex(ValueError,'active clients'):verify_target('NODE-1',data)

    def test_active_or_restartable_crm_is_rejected(self):
        for changes in [{'crm_active':'active'},{'crm_unit':'enabled'}]:
            data=healthy();data['NODE-1'].update(changes)
            with self.assertRaisesRegex(ValueError,'CRM must be'):
                verify_target('NODE-1',data)

    def test_other_observer_reporting_target_master_is_rejected(self):
        data=healthy();data['NODE-2']['ha']=data['NODE-2']['ha'].replace('master NODE-2','master NODE-1')
        with self.assertRaisesRegex(ValueError,'HA master'):verify_target('NODE-1',data)

    def test_maintenance_label_with_armed_watchdog_is_rejected(self):
        data=healthy()
        data['NODE-1']['ha']=data['NODE-1']['ha'].replace('maintenance, watchdog standby','maintenance, watchdog active')
        data['NODE-1']['watchdog_active']=True
        with self.assertRaisesRegex(ValueError,'active clients'):verify_target('NODE-1',data)

    def test_non_ha_guest_and_unfinished_ha_action_are_rejected(self):
        data=healthy();data['NODE-1']['resources'].append({'type':'lxc','node':'NODE-1','status':'running'})
        with self.assertRaisesRegex(ValueError,'guests'):verify_target('NODE-1',data)
        data=healthy();data['NODE-2']['ha']=data['NODE-2']['ha'].replace('NODE-2, started','NODE-2, freeze')
        with self.assertRaisesRegex(ValueError,'not stable'):verify_target('NODE-1',data)


if __name__=='__main__':unittest.main()
