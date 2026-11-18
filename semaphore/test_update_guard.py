"""Safety decisions using cluster fixtures; never contacts a host."""
import copy
from pathlib import Path
import pathlib
import tempfile
import unittest
from update_guard import WATCHDOG_PROBE, cluster_baseline, verify_cluster, verify_target, verify_selection


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

    def test_monitor_election_is_rejected_after_health_recovers(self):
        data=healthy();baseline=cluster_baseline(data)
        for d in data.values():d['ceph']['election_epoch']+=2
        with self.assertRaisesRegex(ValueError,'election changed'):
            verify_cluster(list(data),data,baseline)

    def test_disagreeing_or_invalid_election_evidence_is_rejected(self):
        for value in [101,0,'100']:
            data=healthy();data['NODE-1']['ceph']['election_epoch']=value
            with self.assertRaisesRegex(ValueError,'election evidence'):
                verify_cluster(list(data),data)

    def test_clean_background_scrub_is_allowed_but_recovery_is_not(self):
        for state in ['active+clean+scrubbing','active+clean+scrubbing+deep']:
            data=healthy();data['NODE-1']['ceph']['pgmap']['pgs_by_state'][0]['state_name']=state
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
