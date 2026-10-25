"""Bounded startup and interrupted rollback retain real health/identity gates."""
import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('startup_controller',Path(__file__).with_name('production-stack.py'))
ps=importlib.util.module_from_spec(spec);spec.loader.exec_module(ps)

class StartupWaitTests(unittest.TestCase):
    def test_only_real_compose_startup_diagnostics_qualify(self):
        for text in ('application not healthy after 2m0s',
                     'container powersystem-stage4-api-1 is unhealthy',
                     'timed out waiting for containers to become healthy'):
            self.assertTrue(ps.api_startup_wait_diagnostic(text))
        for text in ('permission denied', 'image not found',
                     'application not healthy after invalid',
                     'container powersystem-stage4-agent-rag-1 is unhealthy'):
            self.assertFalse(ps.api_startup_wait_diagnostic(text))

    def rows(self,status='starting'):
        return {name:{'Id':name+'-id','Image':'sha256:'+name,'RestartCount':0,
                     'State':{'Running':True,'OOMKilled':False,'StartedAt':'same',
                              'Health':{'Status':status if name=='api' else 'healthy'}}}
                for name in ('api','agent-rag')}

    def test_delayed_api_requires_actual_health(self):
        rows=self.rows();ready=self.rows('healthy')
        with patch.object(ps,'inspect',side_effect=[rows,rows,ready]),patch.object(ps.time,'sleep') as sleep,patch.object(ps.time,'monotonic',return_value=0):
            ps.wait_initial_api({'images':{n:r['Image'] for n,r in rows.items()}},list(rows))
            sleep.assert_called_once_with(2)

    def test_timeout_does_not_accept_unhealthy(self):
        rows=self.rows()
        with patch.object(ps,'inspect',return_value=rows),patch.object(ps.time,'monotonic',side_effect=[0,2]),self.assertRaisesRegex(ps.Blocked,'api_initialization_timeout'):
            ps.wait_initial_api({'images':{n:r['Image'] for n,r in rows.items()}},list(rows),seconds=1)

    def test_oom_restart_wrong_image_and_other_unhealthy_fail_closed(self):
        for change in ('oom','restart','image','rag-health'):
            rows=self.rows();images={n:r['Image'] for n,r in rows.items()}
            if change=='oom':rows['api']['State']['OOMKilled']=True
            if change=='restart':rows['api']['RestartCount']=1
            if change=='image':rows['api']['Image']='unexpected'
            if change=='rag-health':rows['agent-rag']['State']['Health']['Status']='unhealthy'
            with self.subTest(change=change),patch.object(ps,'inspect',return_value=rows),self.assertRaises(ps.Blocked):
                ps.wait_initial_api({'images':images},list(rows))

    def test_instance_change_during_wait_is_rejected(self):
        rows=self.rows();changed=copy.deepcopy(rows);changed['api']['Id']='other'
        with patch.object(ps,'inspect',side_effect=[rows,changed]),self.assertRaisesRegex(ps.Blocked,'startup_instance_changed'):
            ps.wait_initial_api({'images':{n:r['Image'] for n,r in rows.items()}},list(rows))

    def test_parent_reuse_requires_frozen_origin_labels_environment_and_health(self):
        path=Path('/frozen');rows=self.rows('healthy');manifest={'images':{n:r['Image'] for n,r in rows.items()},'origin_environment':'/private/parent.env'}
        for name,row in rows.items():row['Config']={'Env':['SAFE=false'],'Labels':{'com.docker.compose.project':ps.PROJECT,'com.docker.compose.service':name,'com.docker.compose.project.config_files':str(path/'private/compose.json'),'com.docker.compose.project.environment_file':manifest['origin_environment']}}
        config={'services':{n:{'environment':{'SAFE':'false'}} for n in rows}}
        with patch.object(ps,'inspect',return_value=rows),patch.object(ps,'immutable',return_value=manifest),patch.object(ps,'read',return_value=config):
            self.assertTrue(ps.healthy_parent_already_deployed(path,list(rows)))
            for change in ('image','env','label','health','oom'):
                bad=copy.deepcopy(rows)
                if change=='image':bad['api']['Image']='other'
                if change=='env':bad['api']['Config']['Env']=['SAFE=true']
                if change=='label':bad['api']['Config']['Labels']['com.docker.compose.project.config_files']='/other'
                if change=='health':bad['api']['State']['Health']['Status']='unhealthy'
                if change=='oom':bad['api']['State']['OOMKilled']=True
                with self.subTest(change=change),patch.object(ps,'inspect',return_value=bad):self.assertFalse(ps.healthy_parent_already_deployed(path,list(rows)))

if __name__=='__main__':unittest.main()
