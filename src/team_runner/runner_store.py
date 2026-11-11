"""Atomic local persistence and process locks. No Orca mailbox reads here."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
import uuid


def read_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else default


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextmanager
def lock(path, blocking=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise RuntimeError(f'already running: {path.name}') from None
        try:
            yield f.fileno()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def running(state_dir):
    # Status/UI reads must not create state directories or require write access.
    try:
        with (Path(state_dir) / 'runner.lock').open('r') as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(f, fcntl.LOCK_UN)
            return False
    except FileNotFoundError:
        return False


def enqueue(state_dir, action, data=None, request_id=None):
    request_id = request_id or str(uuid.uuid4())
    try:
        uuid.UUID(request_id)
    except (ValueError, TypeError, AttributeError):
        raise ValueError('request_id must be a UUID') from None
    path = Path(state_dir) / 'commands' / (request_id + '.json')
    with lock(Path(state_dir) / 'commands.lock'):
        old = read_json(path)
        if old:
            if old['action'] != action or old['data'] != (data or {}):
                raise ValueError('request_id already used for another command')
            return old
        record = dict(id=request_id, action=action, data=data or {}, status='pending')
        atomic_json(path, record)
        return record
