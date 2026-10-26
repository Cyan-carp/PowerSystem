"""Machine identities, source registration and scoped VM authorization."""
import copy
import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('host_profile_test',Path(__file__).with_name('host-profile.py'))
hp=importlib.util.module_from_spec(spec)
spec.loader.exec_module(hp)


class HostProfileTests(unittest.TestCase):
    def setUp(self):
        self.source={'host_id':'r730xd-source','hostname':'R730XD',
            'machine_id_sha256':'a'*64,'dmi_uuid':'source-uuid','virtualization':'none',
            'enabled':True,'profile':'powersystem-r0','project_root':'/opt/powersystem',
            'capabilities':['release','backup','restore','registry','observe','migration']}
        self.vm={**self.source,'host_id':'powersystem-prod-vm117','hostname':'powersystem-prod',
                 'machine_id_sha256':'b'*64,'dmi_uuid':'vm117-uuid','virtualization':'kvm'}
        self.registry={'format':1,'project':'powersystem-stage4','hosts':[self.source,self.vm]}

    def test_source_and_new_vm_are_registered_separately(self):
        for host in (self.source,self.vm):
            self.assertEqual(hp.select_profile(self.registry,host,'backup')['host_id'],host['host_id'])

    def test_hostname_alone_cannot_authorize_another_machine(self):
        for field in ('machine_id_sha256','dmi_uuid','virtualization'):
            wrong={**self.vm,field:'different'}
            with self.subTest(field=field),self.assertRaisesRegex(hp.HostError,'machine_identity'):
                hp.select_profile(self.registry,wrong,'release')

    def test_unregistered_host_and_duplicate_match_rejected(self):
        with self.assertRaises(hp.HostError):
            hp.select_profile(self.registry,{**self.vm,'hostname':'unregistered'},'restore')
        doc=copy.deepcopy(self.registry);doc['hosts'].append(copy.deepcopy(self.vm))
        with self.assertRaises(hp.HostError):
            hp.select_profile(doc,self.vm,'release')

    def test_disabled_or_wrong_scope_cannot_authorize(self):
        for field,value in [('enabled',False),('profile','write-enabled'),
                            ('project_root','/another-project'),('capabilities',['backup'])]:
            doc=copy.deepcopy(self.registry);doc['hosts'][1][field]=value
            with self.subTest(field=field),self.assertRaises(hp.HostError):
                hp.select_profile(doc,self.vm,'migration')

    def test_unknown_capability_and_other_project_rejected(self):
        with self.assertRaises(hp.HostError):hp.select_profile(self.registry,self.vm,'enable-actions')
        with self.assertRaises(hp.HostError):
            hp.select_profile({**self.registry,'project':'other'},self.vm,'backup')


if __name__=='__main__':unittest.main()
