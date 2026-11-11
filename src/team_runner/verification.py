"""Frozen input manifests and conservative verification evidence (stdlib only)."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import sys
import uuid

from .runner_store import atomic_json, read_json

DIGEST = re.compile(r"[0-9a-f]{64}\Z")
VERSION = 1


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def inside(checkout, name):
    if not isinstance(name, str) or not name or Path(name).is_absolute() or '..' in Path(name).parts:
        raise ValueError('invalid checkout path: ' + str(name))
    base = Path(checkout).resolve(strict=True)
    path = base / name
    if not path.resolve().is_relative_to(base):
        raise ValueError('path escapes checkout: ' + name)
    if path.is_symlink():
        raise ValueError('manifest requires regular files: ' + name)
    return path


def inventory(checkout, roots):
    if not isinstance(roots, list) or not roots or len(set(roots)) != len(roots):
        raise ValueError('nonempty unique roots required')
    for root in roots:
        if not inside(checkout, root).exists():
            raise ValueError('missing manifest root: ' + root)
    # Git ignore rules omit build/cache artifacts, never tracked tests/dependencies.
    result = subprocess.run(['git', '--literal-pathspecs', '-C', str(checkout), 'ls-files', '-z', '--cached',
                             '--others', '--exclude-standard', '--', *roots],
                            capture_output=True, check=True)
    files = {}
    for raw in sorted(set(result.stdout.split(b'\0')) - {b''}):
        name = os.fsdecode(raw)
        path = inside(checkout, name)
        if not path.is_file():
            raise ValueError('missing/nonregular file: ' + name)
        files[name] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                       'executable': bool(path.stat().st_mode & 0o111)}
    if not files:
        raise ValueError('empty manifest')
    return files


def create(checkout, roots, output):
    value = {'schema_version': VERSION, 'roots': roots, 'files': inventory(checkout, roots)}
    atomic_json(output, value)
    return {'result': 'PASS', 'manifest': str(output),
            'sha256': hashlib.sha256(Path(output).read_bytes()).hexdigest()}


def verify(checkout, manifest, expected, copy=None):
    result = {'result': 'FAIL', 'manifest': str(manifest), 'expected_sha256': expected, 'errors': []}
    try:
        if not isinstance(expected, str) or not DIGEST.fullmatch(expected):
            raise ValueError('expected SHA-256 must be 64 lowercase hexadecimal characters')
        raw = Path(manifest).read_bytes()
        result['sha256'] = hashlib.sha256(raw).hexdigest()
        if result['sha256'] != expected:
            raise ValueError('manifest digest mismatch')
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {'schema_version', 'roots', 'files'}
                or value['schema_version'] != VERSION or not isinstance(value['files'], dict)):
            raise ValueError('invalid manifest format')
        for name, entry in value['files'].items():
            inside(checkout, name)
            if (not isinstance(entry, dict) or set(entry) != {'sha256', 'executable'}
                    or not isinstance(entry['sha256'], str) or not DIGEST.fullmatch(entry['sha256'])
                    or type(entry['executable']) is not bool):
                raise ValueError('invalid file digest/mode: ' + name)
        current = inventory(checkout, value['roots'])
        for name in sorted(set(current) | set(value['files'])):
            if current.get(name) != value['files'].get(name):
                result['errors'].append('source changed/added/deleted: ' + name)
        if copy:
            # Copy must carry its own Git inventory, so additions cannot go unnoticed.
            copied = inventory(copy, value['roots'])
            for name in sorted(set(copied) | set(value['files'])):
                if copied.get(name) != value['files'].get(name):
                    result['errors'].append('test copy changed/added/deleted: ' + name)
        result['inputs'] = digest(value)
        result['roots'] = value['roots']
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        result['errors'].append(str(exc))
    if not result['errors']:
        result['result'] = 'PASS'
    return result


def execution_environment(environment):
    # Deliberately omit ambient flags, credentials, attempt/Orca IDs and shell state.
    base = {'PATH': str(Path(sys.executable).parent) + ':/usr/local/go/bin:/usr/local/bin:/usr/bin:/bin',
            'HOME': str(Path.home()), 'LANG': 'C.UTF-8', 'TMPDIR': '/tmp'}
    return dict(base, **{k: str(v) for k, v in environment.items()})


def evidence_inputs(preflight, command, toolchain, environment, scope_known):
    if preflight.get('result') != 'PASS' or not scope_known:
        raise ValueError('verified inputs and known dependency scope required')
    if not toolchain or not isinstance(environment, dict):
        raise ValueError('toolchain and non-secret environment parameters required')
    # Store only explicitly supplied non-secret parameters, never os.environ.
    if any(re.search(r'key|secret|token|password|dsn|credential', k, re.I) for k in environment):
        raise ValueError('secret environment parameter forbidden')
    execution_env = execution_environment(environment)
    executable = shutil.which(command[0], path=execution_env.get('PATH'))
    if not executable:
        raise ValueError('verification executable unavailable')
    tools = {}
    for name in ('go', 'node', 'npm', 'psql'):
        binary = shutil.which(name, path=execution_env['PATH'])
        tools[name] = hashlib.sha256(Path(binary).read_bytes()).hexdigest() if binary else None
    harness = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('*.py')}
    return {'manifest_inputs': preflight['inputs'], 'command': command,
            'harness': digest(harness), 'toolchain': toolchain, 'tools': tools,
            'executable_sha256': hashlib.sha256(Path(executable).read_bytes()).hexdigest(),
            'environment': environment, 'environment_fingerprint': digest(execution_env)}


def reuse(source, inputs, attempt, output):
    old = read_json(source, {})
    valid = (old.get('result') == 'PASS' and old.get('status') in ('executed', 'reused')
             and bool(old.get('id')) and bool(old.get('logs')) and old.get('inputs') == inputs
             and old.get('postflight') == inputs.get('manifest_inputs'))
    # Logs are part of the evidence, and must still exist unchanged.
    for log in old.get('logs', []):
        try:
            valid = valid and hashlib.sha256(Path(log['path']).read_bytes()).hexdigest() == log['sha256']
        except (OSError, KeyError):
            valid = False
    value = {'id': str(uuid.uuid4()), 'attempt': attempt, 'inputs': inputs,
             'result': 'PASS' if valid else 'REQUIRED',
             'status': 'reused' if valid else 'required', 'source': str(source),
             'postflight': old.get('postflight') if valid else None,
             'logs': old.get('logs', []) if valid else [], 'created_at': time.time()}
    if Path(output).exists():
        raise ValueError('evidence output already exists')
    atomic_json(output, value)
    return value


def gate_valid(checkout, item, current=False):
    if item.get('check_scope') != 'stage':
        return True
    try:
        proof = item['gate_evidence']
        result = read_json(inside(checkout, proof['path']), {})
        manifest = inside(checkout, proof['manifest'])
        raw = manifest.read_bytes()
        if not DIGEST.fullmatch(proof['sha256']) or hashlib.sha256(raw).hexdigest() != proof['sha256']:
            return False
        frozen = json.loads(raw)
        inputs = result.get('inputs', {})
        if (result.get('result') != 'PASS' or result.get('status') not in ('executed', 'reused')
                or not result.get('logs') or inputs.get('command') != item['gate_command']
                or frozen['roots'] != item['gate_roots'] or inputs.get('manifest_inputs') != digest(frozen)
                or result.get('postflight') != digest(frozen)):
            return False
        for log in result['logs']:
            if hashlib.sha256(Path(log['path']).read_bytes()).hexdigest() != log['sha256']:
                return False
        # Accepted historical boundaries stay accepted when the next stage changes code.
        # Runner verifies the live candidate before it writes the acceptance receipt.
        if not current:
            receipt_path = Path(proof['accepted'])
            if not receipt_path.is_absolute():
                receipt_path = inside(checkout, proof['accepted'])
            receipt = read_json(receipt_path, {})
            return receipt == {'manifest': proof['sha256'], 'evidence': digest(result), 'item': item['id']}
        checked = verify(checkout, manifest, proof['sha256'])
        return checked['result'] == 'PASS' and inputs == evidence_inputs(
            checked, item['gate_command'], inputs['toolchain'], inputs['environment'], True)
    except (ValueError, OSError, KeyError, TypeError):
        return False


def accept_gate(checkout, item):
    if not gate_valid(checkout, item, current=True):
        raise ValueError('stage gate is missing, failed or stale')
    proof = item['gate_evidence']
    result = read_json(inside(checkout, proof['path']), {})
    path = Path(proof['accepted'])
    if not path.is_absolute():
        path = inside(checkout, proof['accepted'])
    receipt = {'manifest': proof['sha256'], 'evidence': digest(result), 'item': item['id']}
    if path.exists() and read_json(path) != receipt:
        raise ValueError('accepted stage boundary is immutable; use a new receipt path')
    atomic_json(path, receipt)


def run_check(checkout, manifest, expected, command, toolchain, environment, attempt, output,
              heavy=False, copy=None):
    from contextlib import nullcontext
    from .heavy_resource import acquire
    preflight = verify(checkout, manifest, expected, copy)
    atomic_json(str(output) + '.preflight.json', preflight)
    inputs = evidence_inputs(preflight, command, toolchain, environment, True)
    output = Path(output)
    if output.exists():
        raise ValueError('evidence output already exists; never overwrite a result')
    record = {'id': str(uuid.uuid4()), 'attempt': attempt, 'inputs': inputs, 'result': 'UNKNOWN',
              'status': 'required', 'logs': [], 'created_at': time.time()}
    atomic_json(output, record)
    lease = acquire() if heavy else nullcontext(({}, None, None))
    with lease as (resource, fd, metadata):
        # Recheck after waiting for the host resource.
        fresh = verify(checkout, manifest, expected, copy)
        if fresh['result'] != 'PASS':
            raise ValueError('inputs changed while waiting for resource')
        started = time.monotonic()
        log = Path(str(output) + '.log')
        with log.open('xb') as stream:
            proc = subprocess.Popen(command, cwd=copy or checkout, stdout=stream, stderr=subprocess.STDOUT,
                                    env=execution_environment(environment),
                                    start_new_session=True, pass_fds=(fd,) if fd is not None else ())
            if metadata:
                from .runner_entry import process_stamp
                resource['processes'].append({'pid': proc.pid, 'stamp': process_stamp(proc.pid)})
                # Generic commands may daemonize. The harness must supply cleanup proof;
                # absence leaves the lease fenced instead of assuming children departed.
                atomic_json(metadata, resource)
            code = proc.wait()
        postflight = verify(checkout, manifest, expected, copy)
        atomic_json(str(output) + '.postflight.json', postflight)
        record.update(result='PASS' if code == 0 and postflight['result'] == 'PASS' else 'FAIL',
                      status='executed', exit_code=code, postflight=postflight.get('inputs')
                      if postflight['result'] == 'PASS' else None,
                      execution_seconds=time.monotonic() - started,
                      wait_seconds=resource.get('wait_seconds', 0),
                      logs=[{'path': str(log.resolve()), 'sha256': hashlib.sha256(log.read_bytes()).hexdigest()}])
        # A composite harness may own its DB lease internally (never nest flock).
        # Its structured reports are diagnostics only; they cannot grant PASS.
        resources = []
        for line in log.read_text(errors='replace').splitlines():
            if not line.startswith('{'):
                continue
            try:
                value = json.loads(line).get('team_runner_resource')
                wait = value['wait_seconds']
                if type(wait) in (int, float) and math.isfinite(wait) and wait >= 0:
                    resources.append(value)
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
        nested_wait = sum(r['wait_seconds'] for r in resources)
        record['resources'] = resources
        record['wait_seconds'] += nested_wait
        record['execution_seconds'] = max(0, record['execution_seconds'] - nested_wait)
        # Keep the durable record UNKNOWN until the cleanup lease has settled.
    if heavy and resource.get('status') != 'clean':
        record.update(result='FAIL', cleanup_error='heavy resource cleanup unverified')
    if heavy:
        record['resource'] = resource
    atomic_json(output, record)
    return record


def view(attempt, checks, scope='item'):
    records = []
    directory = Path(attempt) / 'verification' if attempt else None
    if directory and directory.is_dir():
        for path in sorted(directory.glob('*.json')):
            record = read_json(path, {})
            if 'inputs' in record and 'status' in record:
                records.append(record)
    statuses = []
    for check in checks:
        matching = [r for r in records if r.get('inputs', {}).get('command') == check['argv']]
        status = ('not_applicable' if check.get('scope', 'item') != scope else
                  matching[-1]['status'] if matching else 'required')
        statuses.append({'command': check['argv'], 'status': status})
    return {'checks': statuses, 'records': records,
            'resource_wait_seconds': sum(r.get('wait_seconds', 0) for r in records),
            'execution_seconds': sum(r.get('execution_seconds', 0) for r in records),
            'repeats': len(records) - len({digest(r.get('inputs', {}).get('command')) for r in records})}
