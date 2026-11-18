"""Exercise maintenance ownership and failures with an in-memory cluster."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from maintenance import Maintenance, check_plan
from test_update_guard import healthy
from update_guard import cluster_baseline

GiB=1024**3


class Cluster:
    def __init__(self):
        self.data=healthy()
        for d in self.data.values():
            d.update(manager={},active_tasks=[],ha_config=[{'sid':'vm:100'}],crm_commands='')
            d['ha']=d['ha'].replace('maintenance, watchdog standby','active, watchdog active')
            d['resources']=[{'type':'node','node':n,'maxmem':64*GiB,'mem':8*GiB} for n in self.data]
            d['resources'].append({'type':'qemu','node':'NODE-1','vmid':100,'status':'running','maxmem':4*GiB})
        self.data['NODE-1'].update(crm_active='active',crm_unit='enabled',watchdog_active=True)
        self.commands=[]
        self.underlying_units={}
        self.fail=None
        self.evacuate=True

    def execute(self,node,args):
        self.commands.append(args)
        if self.fail and self.fail in args:raise ValueError('injected '+self.fail+' failure')
        if args[0]=='ha-manager':
            enabling=args[-2]=='enable'
            for d in self.data.values():
                if enabling:
                    d['manager']={'node_request':{'NODE-1':{'maintenance':1}}}
                    if self.evacuate:
                        d['ha']=d['ha'].replace('lrm NODE-1 (active, watchdog active','lrm NODE-1 (maintenance, watchdog standby')
                        for r in d['resources']:
                            if r.get('vmid')==100:r['node']='NODE-2'
                else:
                    d['manager']={}
                    d['ha']=d['ha'].replace('lrm NODE-1 (maintenance, watchdog standby','lrm NODE-1 (active, watchdog active')
        elif args[1]=='stop':self.data[node].update(crm_active='inactive',watchdog_active=False)
        elif args[1]=='mask':
            self.underlying_units[node]=self.data[node]['crm_unit']
            self.data[node]['crm_unit']='masked-runtime'
        elif args[1]=='unmask':self.data[node]['crm_unit']=self.underlying_units.pop(node)
        elif args[1]=='start':self.data[node]['crm_active']='active'


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cluster=Cluster()
        self.path=Path(self.tmp.name)/'node.json'
        self.baseline=cluster_baseline(self.cluster.data)
        self.helper=Maintenance(list(self.cluster.data),'NODE-1',self.path,self.baseline,
                                collect=lambda:copy.deepcopy(self.cluster.data),execute=self.cluster.execute,
                                sleep=lambda _:None,clock=lambda:0,timeout=1)

    def test_complete_preparation_and_restore_in_safe_order(self):
        self.helper.prepare()
        self.assertEqual(json.loads(self.path.read_text())['phase'],'prepared')
        self.assertEqual(self.cluster.commands,[
            ['ha-manager','crm-command','node-maintenance','enable','NODE-1'],
            ['systemctl','stop','pve-ha-crm'],['systemctl','mask','--runtime','pve-ha-crm']])
        self.helper.restore()
        self.assertEqual(json.loads(self.path.read_text())['phase'],'restored')
        self.assertEqual(self.cluster.commands[-3:],[['systemctl','unmask','--runtime','pve-ha-crm'],
            ['systemctl','start','pve-ha-crm'],['ha-manager','crm-command','node-maintenance','disable','NODE-1']])

    def test_installed_proxmox_maintenance_mode_prepares_and_restores(self):
        execute=self.cluster.execute
        def installed_status(node,args):
            for d in self.cluster.data.values():
                d['ha']=d['ha'].replace('maintenance mode, watchdog active','maintenance, watchdog standby')
            execute(node,args)
            for d in self.cluster.data.values():
                d['ha']=d['ha'].replace('maintenance, watchdog standby','maintenance mode, watchdog active')
        self.helper.execute=installed_status
        self.helper.prepare()
        self.helper.restore()
        self.assertEqual(json.loads(self.path.read_text())['phase'],'restored')
        self.assertFalse(self.cluster.data['NODE-1']['manager'])

    def test_preview_plan_never_changes_services_or_state(self):
        self.helper.plan()
        self.assertEqual(self.cluster.commands,[])
        self.assertFalse(self.path.exists())

    def test_non_ha_guest_capacity_and_active_task_block_before_changes(self):
        for scenario in ['non-ha','capacity','task','other-maintenance','pending-command']:
            with self.subTest(scenario=scenario):
                c=Cluster()
                if scenario=='non-ha':c.data['NODE-1']['ha_config']=[]
                if scenario=='capacity':c.data['NODE-1']['resources'][1]['mem']=63*GiB
                if scenario=='task':c.data['NODE-1']['active_tasks']=[{'type':'vzdump'}]
                if scenario=='other-maintenance':c.data['NODE-2']['manager']={'node_request':{'NODE-2':{'maintenance':1}}}
                if scenario=='pending-command':c.data['NODE-1']['crm_commands']='enable-node-maintenance NODE-2'
                with self.assertRaises(ValueError):check_plan(list(c.data),'NODE-1',c.data)
                self.assertEqual(c.commands,[])

    def test_failed_evacuation_never_stops_crm(self):
        self.cluster.evacuate=False
        times=iter([0,2]);self.helper.clock=lambda:next(times)
        with self.assertRaisesRegex(ValueError,'Timed out'):self.helper.prepare()
        self.assertEqual(len(self.cluster.commands),1)
        self.assertTrue(json.loads(self.path.read_text())['maintenance_owned'])

    def test_failed_crm_stop_does_not_mask_or_restore(self):
        self.cluster.fail='stop'
        with self.assertRaisesRegex(ValueError,'stop failure'):self.helper.prepare()
        self.assertFalse(any('mask' in args for args in self.cluster.commands))
        self.assertEqual(json.loads(self.path.read_text())['phase'],'stopping_crm')

    def test_unverified_or_connected_watchdog_never_allows_masking(self):
        for changes in [{'watchdog_active':True},{'watchdog_verified':False}]:
            with self.subTest(changes=changes):
                self.setUp()
                execute=self.cluster.execute
                def stopped_with_unsafe_watchdog(node,args):
                    execute(node,args)
                    if args[0]=='systemctl' and args[1]=='stop':self.cluster.data[node].update(changes)
                self.helper.execute=stopped_with_unsafe_watchdog
                times=iter([0,0,0,2]);self.helper.clock=lambda:next(times)
                with self.assertRaisesRegex(ValueError,'Timed out'):self.helper.prepare()
                self.assertFalse(any('mask' in args for args in self.cluster.commands))

    def test_preexisting_maintenance_and_mask_are_preserved(self):
        self.cluster.execute('NODE-1',['ha-manager','crm-command','node-maintenance','enable','NODE-1'])
        self.cluster.data['NODE-1'].update(crm_active='inactive',crm_unit='masked',watchdog_active=False)
        self.cluster.commands=[]
        self.helper.prepare();self.helper.restore()
        self.assertEqual(self.cluster.commands,[])
        self.assertEqual(self.cluster.data['NODE-1']['crm_unit'],'masked')

    def test_unexpected_reboot_and_failed_health_block_restore(self):
        for scenario in ['reboot','ceph']:
            with self.subTest(scenario=scenario):
                self.setUp();self.helper.prepare();before=len(self.cluster.commands)
                if scenario=='reboot':self.cluster.data['NODE-1']['boot_id']='ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee'
                else:self.cluster.data['NODE-2']['ceph']['health']['status']='HEALTH_WARN'
                with self.assertRaises(ValueError):self.helper.restore()
                self.assertEqual(len(self.cluster.commands),before)

    def test_externally_changed_mask_is_not_removed(self):
        self.helper.prepare();before=len(self.cluster.commands)
        self.cluster.data['NODE-1']['crm_unit']='masked'
        with self.assertRaisesRegex(ValueError,'changed externally'):self.helper.restore()
        self.assertEqual(len(self.cluster.commands),before)

    def test_preexisting_disabled_crm_is_started_then_restored_disabled(self):
        self.cluster.data['NODE-1']['crm_unit']='disabled'
        self.helper.prepare();self.helper.restore()
        # unmask never enables the service; it only removes the runtime mask.
        self.assertFalse(any('enable' in a and a[0]=='systemctl' for a in self.cluster.commands))


if __name__=='__main__':unittest.main()
