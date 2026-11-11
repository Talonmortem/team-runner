"""Host-wide heavy-check lease. Crash residue requires explicit process/port proof."""
from contextlib import contextmanager
import os
import ctypes
from pathlib import Path
import socket
import time

from .runner_store import lock, read_json, atomic_json
from .runner_entry import process_stamp


def default_path():
    # Deliberately independent of project/workspace and XDG overrides.
    return Path('/tmp') / ('team-runner-heavy-' + str(os.getuid()) + '.lock')


def listening(port):
    with socket.socket() as sock:
        sock.settimeout(.2)
        return sock.connect_ex(('127.0.0.1', port)) == 0


def clear_residue(value):
    # An incomplete inventory is never proof of cleanup.
    if not value.get('cleanup_inventory_complete'):
        return False
    for proc in value.get('processes', []):
        if process_stamp(proc['pid']) == proc['stamp']:
            return False
    return not any(listening(p) for p in value.get('ports', []))


@contextmanager
def acquire(path=None):
    path = Path(path) if path else default_path()
    started = time.monotonic()
    with subreaper(), lock(path) as fd:
        metadata = Path(str(path) + '.json')
        previous = read_json(metadata, {})
        if previous and previous.get('status') != 'clean' and not clear_residue(previous):
            raise RuntimeError('heavy-check crash residue unverified: ' + str(metadata))
        receipt = {'status': 'running', 'pid': os.getpid(), 'stamp': process_stamp(os.getpid()),
                   'wait_seconds': time.monotonic() - started, 'started_at': time.time(),
                   'load_start': os.getloadavg(), 'cleanup_inventory_complete': False,
                   'processes': [], 'ports': []}
        atomic_json(metadata, receipt)
        before = owned_processes()
        try:
            yield receipt, fd, metadata
        finally:
            receipt['registered_processes'] = list(receipt['processes'])
            receipt['processes'] = [{'pid': pid, 'stamp': stamp} for pid, stamp in owned_processes().items()
                                    if before.get(pid) != stamp]
            receipt['cleanup_inventory_complete'] = True
            receipt.update(execution_seconds=time.time() - receipt['started_at'], load_end=os.getloadavg())
            if clear_residue(receipt):
                receipt['status'] = 'clean'
            else:
                receipt['status'] = 'cleanup_required'
            atomic_json(metadata, receipt)


def process_group(pgid):
    result = []
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            text = path.read_text().split(') ', 1)[1].split()
            if int(text[2]) == pgid and text[0] != 'Z':
                pid = int(path.parent.name)
                result.append({'pid': pid, 'stamp': process_stamp(pid)})
        except (OSError, ValueError, IndexError):
            continue
    return result



def owned_processes():
    """Descendants including double-forked children adopted by our subreaper."""
    parents = {}
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            fields = path.read_text().split(') ', 1)[1].split()
            if fields[0] != 'Z':
                parents[int(path.parent.name)] = int(fields[1])
        except (OSError, ValueError, IndexError):
            continue
    owned = {os.getpid()}
    while True:
        children = {pid for pid, parent in parents.items() if parent in owned}
        if children <= owned:
            break
        owned.update(children)
    return {pid: process_stamp(pid) for pid in owned - {os.getpid()}}


@contextmanager
def subreaper():
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) != 0 or libc.prctl(36, 1, 0, 0, 0) != 0:
        raise RuntimeError('cannot establish heavy-check descendant ownership')
    try:
        yield
    finally:
        libc.prctl(36, previous.value, 0, 0, 0)
