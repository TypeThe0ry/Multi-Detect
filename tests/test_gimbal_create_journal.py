import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from tempfile import TemporaryDirectory

from scripts import gimbal_create_journal as j


class JournalTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'MD_GIMBAL_INVOCATION': 'a' * 32, 'MD_GIMBAL_JOURNAL': self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.command = ['docker', 'run', '-d', '--name', 'md-sim-teacher-slot-0-test', '--label', 'md.gimbal.invocation=' + 'a' * 32, '--memory=64g', '--memory-swap=64g']

    def test_request_precedes_success_and_response_contains_full_id(self):
        def run(*args, **kwargs):
            self.assertEqual(len(list(Path(self.tmp.name).glob('*.request.json'))), 1)
            self.assertEqual(len(list(Path(self.tmp.name).glob('*.response.json'))), 0)
            return SimpleNamespace(returncode=0, stdout='b' * 64 + '\n')
        self.assertEqual(j.run_recorded(self.command, name='md-sim-teacher-slot-0-test', run=run), 'b' * 64)
        request, = Path(self.tmp.name).glob('*.request.json')
        response, = Path(self.tmp.name).glob('*.response.json')
        self.assertEqual(json.loads(request.read_text())['request'], json.loads(response.read_text())['request'])
        self.assertEqual(json.loads(response.read_text())['id'], 'b' * 64)

    def test_unknown_response_leaves_request_unsettled(self):
        def run(*args, **kwargs):
            return SimpleNamespace(returncode=1, stdout='')
        with self.assertRaises(RuntimeError):
            j.run_recorded(self.command, name='md-sim-teacher-slot-0-test', run=run)
        self.assertEqual(len(list(Path(self.tmp.name).glob('*.request.json'))), 1)
        self.assertFalse(list(Path(self.tmp.name).glob('*.response.json')))

    def test_verify_owned_rejects_foreign(self):
        cid = 'b' * 64
        def inspect(*args, **kwargs):
            return SimpleNamespace(returncode=0, stdout=json.dumps([{'Id': cid, 'Name': '/md-sim-teacher-slot-0-x', 'Config': {'Labels': {'md.gimbal.invocation': 'c' * 32}}}]))
        with self.assertRaises(ValueError):
            j.verify_owned(cid, run=inspect, executable='docker')

    def test_real_collector_entry_records_actual_run(self):
        from scripts import collect_sim_teacher_episodes as collector
        plan = SimpleNamespace(attach_container=None, container_prefix='md-sim-teacher-slot-0', image='sha256:test', out=self.tmp.name)
        container = collector.DockerSimEnvironment(plan, 'test')
        def run(command, **kwargs):
            self.assertEqual(command, self.command)
            return SimpleNamespace(returncode=0, stdout='d' * 64 + '\n')
        with patch.object(collector, 'freeze_collection_runtime', return_value=plan), patch.object(collector, 'docker_command', return_value=self.command), patch.object(collector.subprocess, 'run', side_effect=run):
            self.assertIs(container.__enter__(), container)
        self.assertEqual(container._candidate_id, 'd' * 64)
        self.assertTrue(container.started)
        self.assertEqual(len(list(Path(self.tmp.name).glob('*.response.json'))), 1)

    def test_actual_builder_through_collector(self):
        from scripts import collect_sim_teacher_episodes as c
        from scripts import run_simulator_docker as b
        plan = SimpleNamespace(container_prefix='md-sim-teacher-slot-0', scenario=str(b.DEFAULT_SCENARIO), out=self.tmp.name, control_port=14560, telemetry_port=14561, image='sha256:'+'3'*64, sim_network='bridge', sim_instance=0, gpu_render=True, gpu_device=0, gpu_devices='0', worker_index=0, airframe=None, segmentation=False, camera_noise_overlay=False, wind_vehicle_overlay=False)
        with patch.object(b, '_docker_binary', return_value=None), patch.object(c, '_effective_px4_logger_mode', return_value='default'):
            command=c.docker_command(plan, name='md-sim-teacher-slot-0-test')
        self.assertEqual(command[:2], ['docker','run'])
        self.assertIn('-d', command)
        self.assertIn('md.gimbal.invocation='+'a'*32, command)
        self.assertIn('127.0.0.1:14560:14560/udp', command)
        self.assertNotIn('--rm', command)
        self.assertIn('--memory=64g', command)
        self.assertIn('--memory-swap=64g', command)
        self.assertIn('sha256:'+'3'*64, command)

    def test_removal_failure_propagates_and_does_not_record(self):
        from scripts import collect_sim_teacher_episodes as c
        plan=SimpleNamespace(attach_container=None,container_prefix='md-sim-teacher-slot-0',image='image',out=self.tmp.name,keep_container_on_failure=False)
        container=c.DockerSimEnvironment(plan,'test');container.started=True;container._candidate_id='b'*64
        def run(command,**kw):
            if command[1]=='inspect':
                return SimpleNamespace(returncode=0,stdout=json.dumps([{'Id':'b'*64,'Name':'/md-sim-teacher-slot-0-test','Config':{'Labels':{'md.gimbal.invocation':'a'*32}}}]))
            return SimpleNamespace(returncode=1,stdout='')
        with patch.object(c,'_docker_executable',return_value='docker'),patch.object(c.subprocess,'run',side_effect=run),patch.object(container,'capture_cleanup_diagnostics'):
            with self.assertRaises(RuntimeError):container.__exit__(None,None,None)
        self.assertFalse(list(Path(self.tmp.name).glob('*.removed.json')))

    def test_timeout_leaves_unsettled_request(self):
        import subprocess
        def run(*args,**kwargs):raise subprocess.TimeoutExpired('docker',30)
        with self.assertRaises(subprocess.TimeoutExpired):j.run_recorded(self.command,name='md-sim-teacher-slot-0-test',run=run)
        self.assertEqual(len(list(Path(self.tmp.name).glob('*.request.json'))),1)
        self.assertFalse(list(Path(self.tmp.name).glob('*.response.json')))
