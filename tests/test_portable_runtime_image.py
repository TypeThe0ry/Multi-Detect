"""Content verification, cross-store identity, and guarded execution boundaries."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.simulator import runtime_image as runtime
from multidetect.simulator.px4_image import DEFAULT_GPU_IMAGE, PINNED_IMAGE
from scripts import collect_sim_teacher_episodes as collector
from scripts import run_simulator_docker as planner

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / 'configs/scenarios/urban_inspection_multirotor_001.scenario.json'
LOCAL_ID = 'sha256:' + '7' * 64


@pytest.fixture(autouse=True)
def isolated_gpu_policy(monkeypatch):
    monkeypatch.setenv('MULTIDETECT_SIM_GPU_AVOID', '1,2,3')
    monkeypatch.setenv('MULTIDETECT_SIM_GPU_DEVICES', '0')
    monkeypatch.setenv('MULTIDETECT_SIM_CPUSET', 'off')


def inspected(image_id=LOCAL_ID):
    contract = runtime.load_contract()
    return {'Id': image_id, 'RootFS': copy.deepcopy(contract['rootfs']),
            'Config': copy.deepcopy(contract['config']), **contract['platform']}


def fake_runner(row, calls):
    def run(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(returncode=0, stdout=json.dumps([row]), stderr='')
    return run


@pytest.mark.parametrize('local_id', [LOCAL_ID, 'sha256:' + 'a' * 64])
def test_complete_content_can_have_a_different_local_id(local_id):
    calls = []
    receipt = runtime.verify_gpu_runtime_image('imported-image', runner=fake_runner(inspected(local_id), calls))
    assert receipt['image_id'] == local_id and receipt['status'] == 'content_verified'
    assert calls == [(['docker', 'image', 'inspect', 'imported-image'],
                      {'capture_output': True, 'text': True, 'timeout': 15, 'check': False})]
    assert receipt['components']['transport_sha256'].startswith('aff44fe0')


@pytest.mark.parametrize('mutation', ['layer', 'extra-layer', 'config', 'env', 'platform', 'id'])
def test_rejects_correct_binary_claims_inside_modified_runtime(mutation):
    row = inspected()
    if mutation == 'layer': row['RootFS']['Layers'][0] = 'sha256:' + '0' * 64
    elif mutation == 'extra-layer': row['RootFS']['Layers'].append('sha256:' + '0' * 64)
    elif mutation == 'config': row['Config']['Entrypoint'] = ['/bin/false']
    elif mutation == 'env': row['Config']['Env'].append('UNREVIEWED=1')
    elif mutation == 'platform': row['Architecture'] = 'arm64'
    elif mutation == 'id': row['Id'] = 'short-id'
    with pytest.raises(ValueError):
        runtime.verify_gpu_runtime_image(runner=fake_runner(row, []))


@pytest.mark.parametrize('stdout,code', [('', 1), ('not-json', 0), ('[]', 0), ('[{},{}]', 0)])
def test_missing_or_invalid_identity_never_pulls_or_falls_back(stdout, code):
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=code, stdout=stdout, stderr='unavailable')
    with pytest.raises(ValueError): runtime.verify_gpu_runtime_image(runner=run)
    assert len(calls) == 1 and calls[0][1:3] == ['image', 'inspect']


def test_launch_freezes_verified_identity_but_planning_stays_offline(tmp_path, monkeypatch):
    monkeypatch.setenv('MULTIDETECT_SIM_CPUSET', 'off')
    monkeypatch.setenv('MULTIDETECT_SIM_GPU_DEVICES', '0')
    monkeypatch.delenv('MULTIDETECT_SIM_IMAGE', raising=False)
    plan = planner.build_docker_simulator_plan(SCENARIO, job_dir=tmp_path, gpu_render=True)
    assert plan.image == DEFAULT_GPU_IMAGE
    assert plan.to_mapping()['image_identity_status'] == 'planned_reference'
    calls = []
    frozen = planner.freeze_runtime_plan(plan, runner=fake_runner(inspected(), calls))
    assert frozen.image == LOCAL_ID and frozen.requested_image == DEFAULT_GPU_IMAGE
    assert LOCAL_ID in frozen.command and DEFAULT_GPU_IMAGE not in frozen.command
    assert frozen.to_mapping()['runtime_image_verification']['image_id'] == LOCAL_ID
    # No subsequent tag lookup: Docker argv uses the immutable observed identity.
    assert planner.freeze_runtime_plan(frozen) is frozen
    assert len(calls) == 1


def test_explicit_old_override_does_not_claim_candidate_qualification(tmp_path, monkeypatch):
    monkeypatch.setenv('MULTIDETECT_SIM_IMAGE', DEFAULT_GPU_IMAGE)
    plan = planner.build_docker_simulator_plan(SCENARIO, job_dir=tmp_path, gpu_render=True, image=PINNED_IMAGE)
    def never(*a, **kw): raise AssertionError('explicit old A/B must not be relabeled')
    assert planner.freeze_runtime_plan(plan, runner=never) is plan
    assert plan.runtime_image_verification is None


def test_collection_preflight_failure_cannot_launch_or_record_fake_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv('MULTIDETECT_SIM_GPU', '1')
    monkeypatch.setenv('MULTIDETECT_SIM_GPU_DEVICES', '0')
    monkeypatch.delenv('MULTIDETECT_SIM_IMAGE', raising=False)
    plan = collector.CollectionPlan(str(SCENARIO), 'runtime', 'truth', 'explore', 1, 1, 0, str(tmp_path))
    def deny(*a, **kw): raise ValueError('missing qualified runtime')
    monkeypatch.setattr(collector, 'verify_gpu_runtime_image', deny)
    env = collector.DockerSimEnvironment(plan, 'episode-test')
    with pytest.raises(ValueError, match='missing qualified'):
        env.__enter__()
    assert env.started is False


def test_collection_freezes_requested_and_actual_identity(tmp_path, monkeypatch):
    monkeypatch.setenv('MULTIDETECT_SIM_GPU', '1')
    monkeypatch.setenv('MULTIDETECT_SIM_GPU_DEVICES', '0')
    monkeypatch.delenv('MULTIDETECT_SIM_IMAGE', raising=False)
    plan = collector.CollectionPlan(str(SCENARIO), 'runtime', 'truth', 'explore', 1, 1, 0, str(tmp_path))
    receipt = runtime.verify_gpu_runtime_image(runner=fake_runner(inspected(), []))
    monkeypatch.setattr(collector, 'verify_gpu_runtime_image', lambda *a, **kw: receipt)
    frozen = collector.freeze_collection_runtime(plan)
    assert frozen.image == LOCAL_ID and frozen.requested_image == DEFAULT_GPU_IMAGE
    assert frozen.to_mapping()['runtime_image_verification'] == receipt
    attached = collector.CollectionPlan(str(SCENARIO), 'runtime', 'truth', 'explore', 1, 1, 0, str(tmp_path), attach_container='borrowed')
    assert collector.freeze_collection_runtime(attached) is attached
    assert attached.to_mapping()['image'] is None


def test_created_container_metadata_and_raw_header_use_actual_identity(tmp_path, monkeypatch):
    monkeypatch.setenv('MULTIDETECT_SIM_GPU', '1')
    monkeypatch.delenv('MULTIDETECT_SIM_IMAGE', raising=False)
    receipt = runtime.verify_gpu_runtime_image(runner=fake_runner(inspected(), []))
    monkeypatch.setattr(collector, 'verify_gpu_runtime_image', lambda *a, **kw: receipt)
    commands = []
    def run(argv, **kw):
        commands.append(argv)
        return SimpleNamespace(returncode=0, stdout='c' * 64, stderr='')
    monkeypatch.setattr(collector.subprocess, 'run', run)
    plan = collector.CollectionPlan(str(SCENARIO), 'runtime', 'truth', 'explore', 1, 1, 0, str(tmp_path))
    env = collector.DockerSimEnvironment(plan, 'episode-test')
    env.__enter__()
    assert env.started and LOCAL_ID in commands[-1] and DEFAULT_GPU_IMAGE not in commands[-1]
    env._container_snapshot = lambda: {'container_id': 'c' * 64, 'image': LOCAL_ID,
                                     'state': {'running': True}, 'px4_host_pids': []}
    rows = []
    rec = SimpleNamespace(meta={}, episode_path=tmp_path/'episode.json', record_mavlink=rows.append)
    collector._record_container_started(rec, env)
    assert rec.meta['simulator_image'] == rows[0]['image'] == LOCAL_ID
    assert rec.meta['requested_simulator_image'] == rows[0]['requested_image'] == DEFAULT_GPU_IMAGE
    assert rows[0]['image_identity_source'] == 'container_inspect'
    env._container_snapshot = lambda: {'container_id': 'c' * 64, 'image': 'wrong', 'state': {'running': True}}
    with pytest.raises(RuntimeError, match='identity'):
        env.remember_px4_process()


def test_teacher_resolved_plan_rechecks_content_before_accepting_receipt(tmp_path, monkeypatch):
    from dataclasses import replace
    from scripts import onboard_teacher_gazebo_session as teacher
    monkeypatch.setenv('MULTIDETECT_SIM_GPU', '1')
    monkeypatch.delenv('MULTIDETECT_SIM_IMAGE', raising=False)
    session = teacher.build_teacher_session_plan(SCENARIO, tmp_path)
    frozen = planner.freeze_runtime_plan(session.docker_plan, runner=fake_runner(inspected(), []))
    checks = []
    receipt = frozen.runtime_image_verification
    monkeypatch.setattr(runtime, 'verify_gpu_runtime_image', lambda reference: checks.append(reference) or receipt)
    qualified = replace(session, docker_plan=frozen)
    assert qualified.docker_plan.image == LOCAL_ID and checks == [LOCAL_ID]
    def deny(reference): raise ValueError('actual content mismatch')
    monkeypatch.setattr(runtime, 'verify_gpu_runtime_image', deny)
    with pytest.raises(ValueError, match='actual content mismatch'):
        replace(session, docker_plan=frozen)
