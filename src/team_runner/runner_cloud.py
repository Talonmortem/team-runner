"""Cloud credentials are loaded only in the terminal child, never in launch argv."""
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile


def key_path():
    return Path.home() / 'repo/sber/AI/.env'


def cloud_key():
    try:
        lines = key_path().read_text().splitlines()
    except OSError:
        raise ValueError('CLOUD_API file unavailable') from None
    value = None
    for line in lines:
        match = re.match(r'^\s*(?:export\s+)?CLOUD_API\s*=\s*(.*)$', line)
        if not match:
            continue
        try:
            parts = shlex.split(match[1], comments=True, posix=True)
        except ValueError:
            raise ValueError('invalid CLOUD_API assignment') from None
        if len(parts) != 1 or not parts[0].strip():
            raise ValueError('CLOUD_API is empty or invalid')
        value = parts[0]
    if value is None:
        raise ValueError('CLOUD_API is missing')
    return value


def overlay(config):
    effort = config['effort']
    if effort not in ('default', 'medium', 'high', 'xhigh'):
        raise ValueError('unsupported OpenCode effort')
    model = config['model']
    selected = 'cloudru/' + model
    content = {'model': selected, 'default_agent': 'build', 'agents': {'build': {'model': selected}},
               'experimental': {'subagent_depth': 0},
               'permissions': [{'action': 'subagent', 'resource': '*', 'effect': 'deny'}]}
    provider = {'env': ['CLOUD_API'], 'settings': {'apiKey': '{env:CLOUD_API}'}}
    if effort != 'default':
        variant = {'id': effort, 'settings': {'reasoningEffort': effort}}
        if model in ('deepseek-v4.1-flash', 'glm-5.2'):
            if effort not in ('medium', 'high'):
                raise ValueError('this Cloud.ru model requires medium or high effort')
            variant['body'] = {'thinking': {'type': 'enabled'}}
            if model == 'glm-5.2':
                variant['body']['thinking']['clear_thinking'] = False
        provider['models'] = {model: {'settings': {'reasoningEffort': effort}, 'variants': [variant]}}
        if 'body' in variant:
            provider['models'][model]['body'] = variant['body']
            content['agents']['build']['request'] = {'body': variant['body']}
        selected += '#' + effort
        content['model'] = selected
        content['agents']['build']['model'] = selected
    entry = provider.setdefault('models', {}).setdefault(model, {})
    entry['limit'] = {'output': 24576}
    # Model capability metadata alone does not set the outgoing request limit.
    body = {**entry.get('body', {}), 'max_completion_tokens': 24576}
    entry['body'] = body
    content['agents']['build']['request'] = {'body': body}
    content['providers'] = {'cloudru': provider}
    return content


def isolated_state(environment):
    """The interactive TUI prefers persisted variants over the configured model suffix.

    Keep the shared session database, but give each terminal private TUI preferences.
    No secrets are written here. Retain the directory for the exec'ed process lifetime.
    """
    content = json.loads(environment['OPENCODE_CONFIG_CONTENT'])
    selected = content['agents']['build']['model']
    model, _, effort = selected.partition('#')
    provider, model_id = model.split('/', 1)
    shared = Path(environment.get('TEAM_RUNNER_STATE_ROOT',
                                  Path(environment.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'team-runner'))
    # Shell tools must retain the runner namespace while the TUI uses private preferences.
    environment['TEAM_RUNNER_STATE_ROOT'] = str(shared)
    root = shared / 'opencode'
    root.mkdir(parents=True, exist_ok=True)
    state = Path(tempfile.mkdtemp(prefix='launch-', dir=root))
    preferences = state / 'opencode'
    preferences.mkdir()
    (preferences / 'model.json').write_text(json.dumps({
        'recent': [{'providerID': provider, 'modelID': model_id}], 'favorite': [],
        'variant': {model: effort} if effort else {}}))
    environment['XDG_STATE_HOME'] = str(state)


def main():
    from .runner_config import sber_allowed
    if not sber_allowed(Path.cwd()):
        raise ValueError('Cloud.ru requires a checkout under ~/repo/sber')
    environment = dict(os.environ)
    environment['CLOUD_API'] = cloud_key()
    # A still-running runner may supply an overlay created by the previous version.
    content = json.loads(environment['OPENCODE_CONFIG_CONTENT'])
    entries = list(content.get('providers', {}).get('cloudru', {}).get('models', {}).values())
    entries.append(content['agents']['build'].setdefault('request', {}))
    for entry in entries:
        body = entry.setdefault('body', {})
        body.pop('max_tokens', None)
        body['max_completion_tokens'] = 24576
    environment['OPENCODE_CONFIG_CONTENT'] = json.dumps(content)
    isolated_state(environment)
    os.execvpe('opencode2', ['opencode2', *sys.argv[1:]], environment)


if __name__ == '__main__':
    main()
