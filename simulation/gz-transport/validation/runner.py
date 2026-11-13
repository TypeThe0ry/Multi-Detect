"""CPU protocol comparison worker; invoked only in the pinned SDK container."""
from pathlib import Path
import subprocess, os, json, shlex, signal, hashlib, time, uuid, resource, sys, shutil
r = Path(sys.argv[1])
os.chdir(r)
assets = Path(__file__).resolve().parent
pins = json.loads((assets.parent / 'pins.json').read_text())
result = {'boundary': 'component only; no Gazebo/PX4/SITL; Node public subscription/publish APIs; explicit internal OnNewConnection injection; real library RecvMsgUpdate; raw XPUB malformed-frame injection; white-box own S owner', 'cases': []}

def run(argv, label, env=None):
    start = time.monotonic()
    usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    with (r / (label + '.stdout')).open('w') as out, (r / (label + '.stderr')).open('w') as err:
        p = subprocess.Popen(argv, stdout=out, stderr=err, start_new_session=True, env=env)
        timed_out = False
        try:
            p.wait(timeout=7)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(p.pid, signal.SIGKILL)
            p.wait(timeout=1)
    d = {'argv': argv, 'exit_code': p.returncode, 'timeout_s': 7, 'timed_out': timed_out, 'elapsed_s': time.monotonic() - start, 'cpu_user_s': resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime - usage_before.ru_utime, 'cpu_system_s': resource.getrusage(resource.RUSAGE_CHILDREN).ru_stime - usage_before.ru_stime, 'stdout': label + '.stdout', 'stderr': label + '.stderr'}
    return d

def save():
    (r / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
try:
    version = subprocess.check_output(['pkg-config', '--modversion', 'gz-transport13'], text=True, timeout=7).strip()
    if not version == pins['version']:
        raise RuntimeError("Validation check failed: version == pins['version']")
    result['library_version'] = version
    result['compiler'] = subprocess.check_output(['g++', '--version'], text=True, timeout=7).splitlines()[0]
    cflags = shlex.split(subprocess.check_output(['pkg-config', '--cflags', 'gz-transport13'], text=True, timeout=7))
    libs = shlex.split(subprocess.check_output(['pkg-config', '--libs', 'gz-transport13'], text=True, timeout=7))
    result['build_stages'] = []
    for name in ['framing_matrix', 'normal_pair']:
        shutil.copyfile(assets / (name + '.cc'), r / (name + '.cc'))
        for stage, argv in [('preprocess', ['g++', '-std=c++17', '-pthread', '-E', name + '.cc', '-o', name + '.ii', *cflags]), ('compile', ['g++', '-std=c++17', '-O0', '-g0', '-pthread', '-c', name + '.ii', '-o', name + '.o']), ('link', ['g++', '-pthread', name + '.o', '-o', name, *libs, '-lzmq'])]:
            d = run(argv, name + '-' + stage)
            result['build_stages'].append(d)
            save()
            if not d['exit_code'] == 0:
                raise RuntimeError("Validation check failed: d['exit_code'] == 0")
    shutil.copyfile(assets / 'recv_fault.cc', r / 'recv_fault.cc')
    d = run(['g++', '-shared', '-fPIC', '-std=c++17', '-O0', '-g0', '-pthread', 'recv_fault.cc', '-o', 'recv_fault.so', '-ldl'], 'fault-build')
    result['build_stages'].append(d)
    save()
    if not d['exit_code'] == 0:
        raise RuntimeError("Validation check failed: d['exit_code'] == 0")
    d = run(['g++', '-std=c++17', '-O0', '-g0', '-pthread', '/src/src/TopicStatistics_TEST.cc', '-o', 'upstream_statistics', '-lgtest_main', '-lgtest', *cflags, *libs], 'upstream-build')
    result['build_stages'].append(d)
    save()
    if not d['exit_code'] == 0:
        raise RuntimeError("Validation check failed: d['exit_code'] == 0")
    result['binary_sha256'] = {n: hashlib.sha256((r / n).read_bytes()).hexdigest() for n in ['framing_matrix', 'normal_pair', 'recv_fault.so', 'upstream_statistics']}
    result['framing'] = []
    result['official_pubsub'] = []
    result['ldd'] = {}
    cases = ['valid-four', 'complete-one', 'uncommitted-multipart', 'extra-fifth', 'valid-five', 'six', 'three', 'fault-eagain', 'fault-eintr', 'metadata-content']
    for kind in ['legacy', 'fixed']:
        base = os.environ.copy()
        base['LD_PRELOAD'] = ''
        base.update(GZ_IP='127.0.0.1', CUDA_VISIBLE_DEVICES='', NVIDIA_VISIBLE_DEVICES='void', LD_BIND_NOW='1')
        if kind == 'fixed':
            base['LD_LIBRARY_PATH'] = '/candidate-lib'
        else:
            base.pop('LD_LIBRARY_PATH', None)
        ld = subprocess.check_output(['ldd', str(r / 'framing_matrix')], text=True, env=base, timeout=7)
        (r / ('ldd-' + kind + '.txt')).write_text(ld)
        transport = [line.split()[2] for line in ld.splitlines() if 'libgz-transport13.so' in line][0]
        sha = hashlib.sha256(Path(transport).read_bytes()).hexdigest()
        result['ldd'][kind] = {'path': transport, 'sha256': sha}
        if kind == 'legacy':
            if not sha == pins['reference_library_sha256']:
                raise RuntimeError("Validation check failed: sha == pins['reference_library_sha256']")
        elif not sha == pins['validated_library_sha256']:
            raise RuntimeError("Validation check failed: sha == pins['validated_library_sha256']")
        for case in cases:
            env = base.copy()
            env['GZ_PARTITION'] = 'delta_framing_' + uuid.uuid4().hex
            env['GZ_TRANSPORT_TOPIC_STATISTICS'] = '1' if case == 'metadata-content' else '0'
            label = kind + '-' + case
            if case.startswith('fault-'):
                env['DELTA_RECV_FAULT'] = case.split('-')[1]
                env['DELTA_FAULT_LOG'] = str(r / (label + '-fault.json'))
                env['LD_PRELOAD'] = str(r / 'recv_fault.so')
            d = run([str(r / 'framing_matrix'), case, kind], label, env)
            d.update(library=kind, case=case, partition=env['GZ_PARTITION'], stats_environment=env['GZ_TRANSPORT_TOPIC_STATISTICS'])
            try:
                d['observation'] = json.loads((r / d['stdout']).read_text().strip())
            except Exception:
                d['observation_parse_failed'] = True
            if case.startswith('fault-') and Path(env['DELTA_FAULT_LOG']).exists():
                d['fault'] = json.loads(Path(env['DELTA_FAULT_LOG']).read_text())
            result['framing'].append(d)
            save()
    for kind in ['legacy', 'fixed']:
        base = os.environ.copy()
        base.update(GZ_IP='127.0.0.1', CUDA_VISIBLE_DEVICES='', NVIDIA_VISIBLE_DEVICES='void', LD_PRELOAD='')
        if kind == 'fixed':
            base['LD_LIBRARY_PATH'] = '/candidate-lib'
        else:
            base.pop('LD_LIBRARY_PATH', None)
        ld = subprocess.check_output(['ldd', str(r / 'normal_pair')], text=True, env=base, timeout=7)
        path = [x.split()[2] for x in ld.splitlines() if 'libgz-transport13.so' in x][0]
        actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        if not actual == (pins['reference_library_sha256'] if kind == 'legacy' else pins['validated_library_sha256']):
            raise RuntimeError("Validation check failed: actual == (pins['reference_library_sha256'] if kind == 'legacy' else pins['validated_library_sha256'])")
        for ps, ss in [(0, 0), (0, 1), (1, 0), (1, 1)]:
            label = f'{kind}-p{ps}s{ss}'
            partition = 'delta_official_ready_' + uuid.uuid4().hex
            common = base.copy()
            common.update(GZ_PARTITION=partition, DELTA_PAIR_PREFIX=str(r / label))
            processes = []
            for role, stats in [('subscriber', ss), ('publisher', ps)]:
                if role == 'publisher':
                    end = time.monotonic() + 1.5
                    while not Path(common['DELTA_PAIR_PREFIX'] + '.subscriber-ready').exists() and time.monotonic() < end:
                        time.sleep(0.005)
                env = common.copy()
                env['GZ_TRANSPORT_TOPIC_STATISTICS'] = str(stats)
                argv = [str(r / 'normal_pair'), role, 'both-stats' if ps and ss else 'no-stats']
                out = (r / (label + '-' + role + '.stdout')).open('w')
                err = (r / (label + '-' + role + '.stderr')).open('w')
                start = time.monotonic()
                p = subprocess.Popen(argv, stdout=out, stderr=err, start_new_session=True, env=env)
                processes.append((role, p, start, argv, out, err))
            pair = {'library': kind, 'library_sha256': actual, 'publisher_statistics': ps, 'subscriber_statistics': ss, 'partition': partition, 'ready_file': label + '.subscriber-ready', 'ack_file': label + '.warmup-ack', 'processes': []}
            for role, p, start, argv, out, err in processes:
                timeout = False
                try:
                    p.wait(timeout=max(0.001, start + 7 - time.monotonic()))
                except subprocess.TimeoutExpired:
                    timeout = True
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait(timeout=1)
                out.close()
                err.close()
                entry = {'role': role, 'argv': argv, 'exit_code': p.returncode, 'timed_out': timeout, 'timeout_s': 7, 'elapsed_s': time.monotonic() - start, 'stdout': label + '-' + role + '.stdout', 'stderr': label + '-' + role + '.stderr'}
                try:
                    entry['observation'] = json.loads((r / entry['stdout']).read_text().strip())
                except Exception:
                    entry['observation_parse_failed'] = True
                pair['processes'].append(entry)
            pair['desired_wire_compatibility_pass'] = all((x['exit_code'] == 0 and (not x['timed_out']) for x in pair['processes']))
            roles = {x['role']: x for x in pair['processes']}
            pp = roles['publisher']
            ssr = roles['subscriber']
            same = ps == ss
            pair['expected_result_matched'] = pair['desired_wire_compatibility_pass'] if same else not any((x['timed_out'] for x in pair['processes'])) and pp['exit_code'] == 8 and (ssr['exit_code'] == 17) and (pp.get('observation', {}).get('connected') == 0) and (pp['observation'].get('application_warmup_ack') == 0) and (ssr.get('observation', {}).get('received') == 0) and (ssr['observation'].get('warmups_received') == 0)
            result['official_pubsub'].append(pair)
            save()
    result['mechanism_observation_suite_pass'] = all((x['exit_code'] == 0 and (not x['timed_out']) and (x.get('observation', {}).get('expected_mechanism_observation_pass') == 1) and (not x['case'].startswith('fault-') or (x.get('fault', {}).get('injected') == 1 and x['fault']['calls'] <= 16)) for x in result['framing']))
    result['official_version_contract_pass'] = all((x['expected_result_matched'] for x in result['official_pubsub']))
    result['upstream_runs'] = []
    for kind in ['legacy', 'fixed']:
        env = os.environ.copy()
        env.update(GZ_PARTITION='delta_upstream_release_' + uuid.uuid4().hex, GZ_IP='127.0.0.1', CUDA_VISIBLE_DEVICES='', NVIDIA_VISIBLE_DEVICES='void', LD_BIND_NOW='1')
        if kind == 'fixed':
            env['LD_LIBRARY_PATH'] = '/candidate-lib'
        else:
            env.pop('LD_LIBRARY_PATH', None)
        d = run([str(r / 'upstream_statistics'), '--gtest_filter=TopicsStatistics.*', '--gtest_output=xml:/out/upstream-' + kind + '.xml'], 'upstream-' + kind, env)
        result['upstream_runs'].append(d)
        save()
    import xml.etree.ElementTree as ET
    upstream_xml = {kind: ET.parse(r / ('upstream-' + kind + '.xml')).getroot().attrib for kind in ['legacy', 'fixed']}
    result['upstream_xml'] = upstream_xml
    complete = len(result['framing']) == 20 and len(result['official_pubsub']) == 8 and (len(result['upstream_runs']) == 2) and all((int(x.get('tests', 0)) == 4 and all((int(x.get(k, 0)) == 0 for k in ['failures', 'errors', 'disabled'])) for x in upstream_xml.values()))
    result['source_fix_commit'] = pins['frozen_fix_commit']
    result['validation_assets_sha256'] = pins['validation_assets']
    result['component_acceptance_pass'] = complete and result['mechanism_observation_suite_pass'] and result['official_version_contract_pass'] and all((x['exit_code'] == 0 and (not x['timed_out']) for x in result['upstream_runs']))
    result['field_cause_or_flight_acceptance_proven'] = False
    save()
    if not result['component_acceptance_pass']:
        raise RuntimeError("Validation check failed: result['component_acceptance_pass']")
except BaseException as e:
    result['exception'] = repr(e)
    save()
    raise
