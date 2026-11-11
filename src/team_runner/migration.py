"""Read-only cutover gate and checksum-verified archival. No session import."""
import hashlib
import json
import os
from pathlib import Path
import tarfile
from .runner_store import read_json, lock, atomic_json
from .catalog import unsettled


def assert_quiescent(legacy, catalog=None):
    legacy = Path(legacy)
    if unsettled(legacy):
        raise ValueError('legacy runner is active or unknown; reconcile before cutover')
    for path in legacy.rglob('agents.json'):
        data = read_json(path,{})
        if (any(a.get('status') not in ('released','stopped','superseded') for a in data.get('agents',[])) or
                any(r.get('status') != 'closed' for r in data.get('runs',[]))):
            raise ValueError('registered legacy resources are active or unknown: '+str(path))
    if catalog:
        for pid,project in catalog.read()['projects'].items():
            for wid in project['workspaces']:
                if unsettled(catalog.state_dir(pid,wid)):
                    raise ValueError('registered platform attempt active or unknown: '+wid)


def archive(legacy, files, destination, catalog=None):
    """Caller chooses explicit sources. Archive bytes only; never replay queues."""
    legacy,destination = Path(legacy),Path(destination)
    if destination.exists():raise ValueError('archive destination already exists')
    with lock(legacy/'runner.lock',blocking=False), lock(legacy/'start.lock',blocking=False):
        # running() would see our lock. Evaluate state using an explicit lock-free probe
        # before acquiring, then inspect all state fields here without its liveness probe.
        from . import catalog as catalog_module
        # No monkeypatch/global state: quiescence data checks are duplicated minimally below.
        st=read_json(legacy/'state.json',{})
        if st.get('current') or st.get('control_uncertain') or st.get('stop_status')=='unknown':
            raise ValueError('legacy state is active or unknown')
        if read_json(legacy/'startup.json',{}).get('status') in ('starting','unknown'):
            raise ValueError('legacy startup unknown')
        for p in legacy.rglob('operations.json'):
            if any(op.get('status')!='completed' for op in read_json(p,{}).values()):
                raise ValueError('legacy operation unconfirmed')
        for p in legacy.rglob('agents.json'):
            reg=read_json(p,{})
            if any(a.get('status') not in ('released','stopped','superseded') for a in reg.get('agents',[])) or any(r.get('status')!='closed' for r in reg.get('runs',[])):
                raise ValueError('legacy resources not settled')
        if catalog:
            for pid,project in catalog.read()['projects'].items():
                for wid in project['workspaces']:
                    if unsettled(catalog.state_dir(pid,wid)):raise ValueError('platform attempt active or unknown')
        sources = sorted(set([p for p in legacy.rglob('*') if p.is_file()] + [Path(p) for p in files]))
        records=[{'path':str(p.resolve()),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in sources]
        destination.mkdir(parents=True,mode=0o700)
        tarpath=destination/'legacy.tar.gz'
        with tarfile.open(tarpath,'x:gz') as tar:
            for p in sources:tar.add(p,arcname=str(p.resolve()).lstrip('/'),recursive=False)
        with tarfile.open(tarpath,'r:gz') as tar:
            for record in records:
                data=tar.extractfile(record['path'].lstrip('/')).read()
                if hashlib.sha256(data).hexdigest()!=record['sha256'] or hashlib.sha256(Path(record['path']).read_bytes()).hexdigest()!=record['sha256']:
                    raise ValueError('source/archive changed during archival')
        manifest={'legacy_state':str(legacy.resolve()),'files':records,'archive_sha256':hashlib.sha256(tarpath.read_bytes()).hexdigest(),'executable_import':False}
        atomic_json(destination/'manifest.json',manifest)
        for p in destination.iterdir():p.chmod(0o400)
        destination.chmod(0o500)
        return manifest
