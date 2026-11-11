"""Explicit, attempt-frozen cache permissions; no environment copies."""
from pathlib import Path
import tempfile


def validate(value):
    if not isinstance(value, dict) or set(value) - {'go_cache'}:
        raise ValueError('invalid runtime settings')
    cache = value.get('go_cache')
    if cache is not None:
        if not isinstance(cache, str) or not cache or '\x00' in cache:
            raise ValueError('go_cache must be an absolute directory')
        path = Path(cache)
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError('go_cache must be an absolute directory')
        resolved = path.resolve()
        if resolved in (Path('/'), Path.home().resolve()):
            raise ValueError('go_cache must name a dedicated cache directory')
        cache = str(resolved)
    return {'go_cache': cache}


def prepare(value):
    settings = validate(value)
    cache = settings['go_cache']
    if cache:
        path = Path(cache)
        path.mkdir(parents=True, exist_ok=True)
        # Fail before dispatch, rather than letting every worker rediscover EACCES.
        with tempfile.TemporaryFile(dir=path) as handle:
            handle.write(b'team-runner cache preflight')
            handle.flush()
    return settings
