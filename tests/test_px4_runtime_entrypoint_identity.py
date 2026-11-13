"""Review reproductions: normal entrypoints and inspected attachment identity."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.simulator.px4_image import DEFAULT_GPU_IMAGE, PINNED_IMAGE
from scripts import check_sim_gpu_render as check
from scripts import collect_sim_teacher_episodes as collector
from scripts import onboard_teacher_gazebo_session as teacher
from scripts import run_external_onboard_model_acceptance as external
from scripts import run_sim_session as viewer

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT/'configs/scenarios/urban_inspection_multirotor_001.scenario.json'
ACTUAL_IMAGE = 'sha256:'+'8'*64
CONTAINER_ID = 'a'*64


@pytest.fixture(autouse=True)
def gpu_environment(monkeypatch):
    monkeypatch.setenv('MULTIDETECT_SIM_GPU', '1')
    monkeypatch.setenv('MULTIDETECT_SIM_GPU_DEVICES', '0')
    monkeypatch.setenv('MULTIDETECT_SIM_GPU_AVOID', '1,2,3')
    monkeypatch.setenv('MULTIDETECT_SIM_IMAGE', DEFAULT_GPU_IMAGE)
    monkeypatch.setenv('MULTIDETECT_SIM_CPUSET', 'off')


@pytest.mark.parametrize('explicit', [None, PINNED_IMAGE])
@pytest.mark.parametrize('cli', [False, True])
def test_external_default_and_override_reach_real_planner(tmp_path, monkeypatch, explicit, cli):
    plans = []
    build = external.build_docker_simulator_plan
    def capture(*args, **kwargs):
        plan = build(*args, **kwargs)
        plans.append(plan)
        return plan
    monkeypatch.setattr(external, 'build_docker_simulator_plan', capture)
    model = tmp_path/'model.onnx'
    model.write_bytes(b'dry-run-only-no-model-execution')
    if cli:
        args = ['--scenario', str(SCENARIO), '--evidence', str(tmp_path/'evidence.json'), '--model', str(model)]
        if explicit is not None:
            args += ['--image', explicit]
        assert external.main(args) == 0
    else:
        external.run_acceptance(SCENARIO, tmp_path/'evidence.json', model_path=model, image=explicit)
    assert plans[-1].image == (explicit or DEFAULT_GPU_IMAGE)


@pytest.mark.parametrize('explicit', [None, PINNED_IMAGE])
def test_gpu_check_cli_uses_resolver_without_any_real_run(tmp_path, monkeypatch, explicit):
    calls = []
    def fake(command, **kwargs):
        calls.append(list(command))
        if command[1:3] == ['image', 'inspect']:
            from multidetect.simulator.runtime_image import load_contract
            c = load_contract()
            return SimpleNamespace(returncode=0, stdout=json.dumps([{'Id': ACTUAL_IMAGE, 'RootFS': c['rootfs'], 'Config': c['config'], **c['platform']}]), stderr='')
        if command[1:3] == ['inspect', '--format']:
            return SimpleNamespace(returncode=0, stdout='true', stderr='')
        if command[1] == 'exec' and any('eglinfo' in item for item in command):
            return SimpleNamespace(returncode=0, stdout='EGL vendor string: NVIDIA', stderr='')
        return SimpleNamespace(returncode=0, stdout=b'' if command[1]=='exec' else '', stderr=b'' if command[1]=='exec' else '')
    monkeypatch.setattr(check.subprocess, 'run', fake)
    args = ['--scenario', str(SCENARIO), '--job-dir', str(tmp_path/'job'), '--gpu-device', '0', '--frames', '1']
    if explicit is not None:
        args += ['--image', explicit]
    assert check.main(args) == 1  # fake stream has no frame; never claim flight PASS
    launch = next(c for c in calls if c[1:3] == ['run', '-d'])
    assert (explicit or ACTUAL_IMAGE) in launch


def test_teacher_session_accepts_only_qualified_gpu_runtime(tmp_path):
    plan = teacher.build_teacher_session_plan(SCENARIO, tmp_path/'job')
    assert plan.docker_plan.image == DEFAULT_GPU_IMAGE and plan.docker_plan.gpu_render
    with pytest.raises(ValueError, match='qualified GPU runtime'):
        replace(plan, docker_plan=replace(plan.docker_plan, image=ACTUAL_IMAGE))
    with pytest.raises(ValueError, match='qualified GPU runtime'):
        replace(plan, docker_plan=replace(plan.docker_plan, gpu_render=False))
    assert replace(plan, docker_plan=replace(plan.docker_plan, image=PINNED_IMAGE))


def attached_plan(tmp_path):
    return collector.CollectionPlan('scenario', 'runtime', 'truth', 'onboard', 1, 1, 0, str(tmp_path), attach_container='viewer-session')


def inspect_stub(monkeypatch, details):
    calls = []
    def fake(command, **kwargs):
        calls.append(list(command))
        if command[:2] == ['docker', 'inspect']:
            value = 'true' if '{{.State.Running}}' in command else json.dumps(details)
            return SimpleNamespace(returncode=0, stdout=value, stderr='')
        if command[:2] == ['docker', 'top']:
            return SimpleNamespace(returncode=0, stdout='PID COMMAND\n123 px4\n', stderr='')
        raise AssertionError(command)
    monkeypatch.setattr(collector, '_docker_executable', lambda plan: 'docker')
    monkeypatch.setattr(collector.subprocess, 'run', fake)
    return calls


def test_attached_viewer_records_actual_image_in_metadata_and_raw_header(tmp_path, monkeypatch):
    details = {'Id':CONTAINER_ID, 'Image':ACTUAL_IMAGE, 'State':{'Running':True}}
    calls = inspect_stub(monkeypatch, details)
    plan = attached_plan(tmp_path)
    assert plan.image == DEFAULT_GPU_IMAGE  # deliberately misleading ambient expectation
    monkeypatch.setattr(collector, '_takeoff_altitude_for_plan', lambda plan: (10.0, 'test'))
    assert plan.to_mapping()['image'] is None
    rows = []
    recorder = SimpleNamespace(meta={}, episode_path=tmp_path/'episode.json', record_mavlink=rows.append)
    with collector.DockerSimEnvironment(plan, 'test') as environment:
        collector._record_container_started(recorder, environment)
    meta = json.loads(recorder.episode_path.read_text())
    assert meta['simulator_image'] == meta['simulator_container']['image'] == rows[0]['image'] == ACTUAL_IMAGE
    assert meta['requested_simulator_image'] == rows[0]['requested_image'] == DEFAULT_GPU_IMAGE
    assert rows[0]['container_id'] == CONTAINER_ID and rows[0]['image_identity_source']=='container_inspect'
    assert all(c[1] in ('inspect','top') for c in calls)  # never start/remove borrowed container


@pytest.mark.parametrize('details', [
    {'Id':CONTAINER_ID, 'State':{'Running':True}},
    {'Id':CONTAINER_ID, 'Image':'multidetect/viewer:tag', 'State':{'Running':True}},
    {'Image':ACTUAL_IMAGE, 'State':{'Running':True}},
    {'Id':CONTAINER_ID, 'Image':ACTUAL_IMAGE, 'State':{'Running':False}},
])
def test_attach_cannot_claim_default_when_actual_identity_missing(tmp_path, monkeypatch, details):
    calls = inspect_stub(monkeypatch, details)
    with pytest.raises(RuntimeError, match='inspected immutable'):
        with collector.DockerSimEnvironment(attached_plan(tmp_path), 'test'):
            raise AssertionError('Missing identity must fail before collection')
    assert all(c[1] in ('inspect','top') for c in calls)


def test_attach_rejects_container_replacement_before_recording(tmp_path, monkeypatch):
    details = {'Id':CONTAINER_ID, 'Image':ACTUAL_IMAGE, 'State':{'Running':True}}
    inspect_stub(monkeypatch, details)
    with collector.DockerSimEnvironment(attached_plan(tmp_path), 'test') as environment:
        details['Image'] = DEFAULT_GPU_IMAGE
        with pytest.raises(RuntimeError, match='identity changed'):
            environment.remember_px4_process()


def test_viewer_is_software_rendered_even_under_gpu_defaults():
    command, plan, _ = viewer.build_session_command(container_name='viewer-test')
    assert viewer.VIEWER_IMAGE in command and DEFAULT_GPU_IMAGE not in command
    assert not plan.gpu_render and '--gpus' not in command
