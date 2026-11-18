"""Host-wide admission for new agent processes. The queue never launches anything."""
from contextlib import contextmanager
import logging
import math
import os
from pathlib import Path
import stat
import time
import uuid

from .runner_store import atomic_json, lock, read_json

MEMORY_FLOOR_KIB = 4 * 1024**2
MEMORY_FRACTION = .20
MEMORY_MARGIN_KIB = 512 * 1024
MEMORY_MARGIN_FRACTION = .05
PSI_STOP = {'some': 5, 'full': 1}
PSI_RESUME = {'some': 2, 'full': .5}
CHECK_INTERVAL = 2
COOLDOWN = 5
WAIT_EXPIRY = 30
NO_METRICS = 'MemAvailable and memory PSI unavailable; serializing agent launches'


class WaitingMemory(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(status['reason'])


class AdmissionCancelled(RuntimeError):
    pass


class MemoryQueue:
    def __init__(self, directory=None, proc='/proc', clock=time.monotonic, sleep=time.sleep):
        self.directory = Path(directory or f'/tmp/team-runner-memory-{os.getuid()}')
        self.proc = Path(proc)
        self.clock = clock
        self.sleep = sleep

    def _stamp(self, pid):
        try:
            fields = (self.proc / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
            return None if fields[0] in ('Z', 'X') else fields[19]
        except (OSError, IndexError):
            return None

    def _boot(self):
        return (self.proc / 'sys/kernel/random/boot_id').read_text().strip()

    def _private(self):
        self.directory.mkdir(mode=0o700, exist_ok=True)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError(f'agent admission directory must be private: {self.directory}')

    def metrics(self):
        values = {}
        try:
            mem = dict(line.split(':', 1) for line in (self.proc / 'meminfo').read_text().splitlines())
            total, available = (int(mem[key].split()[0]) for key in ('MemTotal', 'MemAvailable'))
            if total > 0 and available >= 0:
                floor = min(MEMORY_FLOOR_KIB, MEMORY_FRACTION * total)
                margin = min(MEMORY_MARGIN_KIB, MEMORY_MARGIN_FRACTION * total)
                values.update(total_kib=total, available_kib=available, floor_kib=floor, resume_kib=floor + margin)
        except (OSError, ValueError, KeyError, IndexError):
            pass
        try:
            lines = (self.proc / 'pressure/memory').read_text().splitlines()
        except OSError:
            lines = []
        for line in lines:
            fields = line.split()
            if fields and fields[0] in PSI_STOP:
                try:
                    value = float(next(field[6:] for field in fields[1:] if field.startswith('avg60=')))
                    if math.isfinite(value) and 0 <= value <= 100:
                        values[fields[0] + '_avg60'] = value
                except (ValueError, StopIteration):
                    pass
        return values

    def _pressure(self, state, now, metrics):
        if not metrics and not state.get('diagnostic'):
            logging.getLogger(__name__).warning(NO_METRICS)
        state['diagnostic'] = None if metrics else NO_METRICS
        available = metrics.get('available_kib', math.inf)
        psi = {signal: metrics.get(signal + '_avg60', 0) for signal in PSI_STOP}
        reasons = []
        if available < metrics.get('floor_kib', 0):
            reasons.append('low_available_memory')
        if any(psi[key] >= threshold for key, threshold in PSI_STOP.items()):
            reasons.append('high_memory_psi')
        if reasons:
            state.update(pressure=reasons, healthy_at=None)
        elif state.get('pressure'):
            if (available < metrics.get('resume_kib', 0)
                    or any(psi[key] >= threshold for key, threshold in PSI_RESUME.items())):
                state['healthy_at'] = None
            elif state.get('healthy_at') is None:
                state['healthy_at'] = now
            elif now - state['healthy_at'] >= CHECK_INTERVAL:
                state.update(pressure=[], healthy_at=None)
        return state.get('pressure', [])

    def _live(self, row):
        return self._stamp(row['pid']) == row['process_stamp']

    def _state(self, boot):
        state = read_json(self.directory / 'queue.json', {})
        return state if state.get('boot_id') == boot else {'boot_id': boot, 'waiting': []}

    def _wait_status(self, row, now):
        return {**row['status'], 'operation': row['operation'], 'attempt': row['attempt'],
                'wait_seconds': max(0, now - row['enqueued'])}

    def _poll(self, owner):
        self._private()
        path = self.directory / 'queue.json'
        with lock(self.directory / 'queue.lock'):
            now = self.clock()
            state = self._state(owner['boot_id'])
            waiting = state['waiting']
            waiting[:] = [row for row in waiting if now - row['updated'] <= WAIT_EXPIRY and self._live(row)]
            holder = state.get('holder')
            if holder and not self._live(holder):
                state.update(holder=None, cooldown_until=now + COOLDOWN)
            row = next((row for row in waiting if all(row[k] == v for k, v in owner.items())), None)
            if row is None:
                row = dict(owner, enqueued=now)
                waiting.append(row)
            row['updated'] = now
            metrics = self.metrics()
            pressure = self._pressure(state, now, metrics)
            reason = (', '.join(pressure) if pressure else
                      'launch_in_progress' if state.get('holder') else
                      'launch_cooldown' if now < state.get('cooldown_until', 0) else
                      'fifo_queue' if row is not waiting[0] else None)
            if reason:
                row['status'] = dict(state='waiting_memory', reason=reason, metrics=metrics,
                                     diagnostic=state['diagnostic'])
                result = (None, self._wait_status(row, now))
            else:
                token = str(uuid.uuid4())
                state['holder'] = dict(owner, token=token)
                waiting.pop(0)
                result = (token, None)
            atomic_json(path, state)
            return result

    def _remove(self, owner, token=None):
        if not self.directory.exists():
            return
        self._private()
        with lock(self.directory / 'queue.lock'):
            state = self._state(owner.get('boot_id') or self._boot())
            holder = state.get('holder')
            if token and holder and holder['token'] == token:
                state.update(holder=None, cooldown_until=self.clock() + COOLDOWN)
            elif token:
                return
            else:
                state['waiting'] = [row for row in state['waiting']
                                    if not all(row[k] == value for k, value in owner.items())]
            atomic_json(self.directory / 'queue.json', state)

    def cancel(self, workspace, attempt):
        if attempt:
            try:
                self._remove(dict(workspace=str(workspace), attempt=str(attempt)))
            except (OSError, RuntimeError, ValueError) as exc:
                logging.getLogger(__name__).warning('cannot cancel agent launch queue entry: %s', exc)

    @contextmanager
    def hold(self, workspace, attempt, operation, *, permitted=None, block=False, between=None):
        pid = os.getpid()
        stamp, boot = self._stamp(pid), self._boot()
        if not stamp or not boot:
            raise RuntimeError('cannot identify agent launch queue owner')
        owner = dict(workspace=str(workspace), attempt=str(attempt), operation=operation,
                     pid=pid, process_stamp=stamp, boot_id=boot)
        token = None
        try:
            while not token:
                if between:
                    between()
                if permitted is not None and not permitted():
                    raise AdmissionCancelled('agent launch cancelled by user intent or attempt change')
                token, status = self._poll(owner)
                if permitted is not None and not permitted():
                    raise AdmissionCancelled('agent launch cancelled by user intent or attempt change')
                if not token:
                    if not block:
                        raise WaitingMemory(status)
                    self.sleep(CHECK_INTERVAL)
            yield
        except WaitingMemory:
            if not token:
                raise
            self._remove(owner, token)
            raise
        except BaseException:
            self._remove(owner, token)
            raise
        else:
            self._remove(owner, token)

    def view(self, workspace, attempt):
        if not self.directory.exists() or not attempt:
            return None
        self._private()
        now = self.clock()
        # atomic_json publishes a complete snapshot. Status readers don't lock or mutate it.
        state = self._state(self._boot())
        row = next((row for row in state['waiting'] if row['workspace'] == str(workspace)
                    and row['attempt'] == str(attempt) and now - row['updated'] <= WAIT_EXPIRY
                    and self._live(row)), None)
        return self._wait_status(row, now) if row else None


queue = MemoryQueue()


def view(workspace, state):
    if state.get('paused') or state.get('stop_requested'):
        return None
    intent = read_json(Path(workspace) / 'goal-intent.json', {})
    if intent and intent.get('desired_state') != 'running':
        return None
    try:
        return queue.view(workspace, (state.get('current') or {}).get('attempt_dir'))
    except (OSError, RuntimeError, ValueError):
        return None
