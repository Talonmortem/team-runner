"""Local Team Runner command line."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from .catalog import Catalog
from .engine import Runner
from .protocol import ContractError


def selection(args, catalog):
    ids = {}
    for key in ('project', 'workspace'):
        inherited = os.environ.get('TEAM_RUNNER_' + key.upper())
        explicit = getattr(args, key, None)
        if inherited and explicit and inherited != explicit:
            raise ValueError('explicit ' + key + ' conflicts with inherited attempt context')
        ids[key] = explicit or inherited
        if not ids[key]: raise ValueError('--project and --workspace required')
    ctx = catalog.context(ids['project'], ids['workspace'], allow_disabled=args.cmd in ('status', 'operation', 'goals', 'messages'))
    for name, expected in [('TEAM_RUNNER_STATE', str(ctx.state_dir)), ('TEAM_RUNNER_CHECKOUT', str(ctx.repo))]:
        if os.environ.get(name) and os.environ[name] != expected:
            raise ValueError(name + ' conflicts with selected workspace')
    attempt = os.environ.get('TEAM_RUNNER_ATTEMPT')
    if attempt:
        ctx.check_attempt(attempt)
        if args.cmd not in ('launch', 'messages', 'reply', 'status', 'events', 'artifacts'):
            raise ValueError('child attempt cannot control workspace lifecycle')
    return Runner(ctx)


class _JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # Unknown option values may contain credentials; do not echo them.
        raise ContractError('invalid_request', 'Invalid command arguments; see --help')


def parser(json_errors=False):
    ap = (_JSONArgumentParser if json_errors else argparse.ArgumentParser)(description=__doc__)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('artifacts')
    p.add_argument('action', choices=['compact', 'restore', 'sweep', 'prepare'])
    p.add_argument('--project'); p.add_argument('--workspace')
    p.add_argument('--run'); p.add_argument('--tree'); p.add_argument('--attempt')
    p.add_argument('--apply', action='store_true')
    p.add_argument('--argv-file'); p.add_argument('--reserve-gib', type=int, default=25)
    p.add_argument('--required-gib', type=int, default=12); p.add_argument('--limit-gib', type=int, default=40)
    p = sub.add_parser('cleanup')
    p.add_argument('--project', required=True); p.add_argument('--workspace', required=True)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--days', type=int, default=7)
    p.add_argument('--legacy-retired', action='store_true', help='include retired ~/.local/state/shm-team/runs')
    for command in ('ui', 'serve'):
        p = sub.add_parser(command); p.add_argument('--port', type=int, default=0); p.add_argument('--no-open', action='store_true')
    p = sub.add_parser('events')
    p.add_argument('action', choices=['list', 'ack'])
    p.add_argument('--project'); p.add_argument('--workspace')
    p.add_argument('--dispatch'); p.add_argument('--ids')
    p = sub.add_parser('manifest')
    p.add_argument('action', choices=['create', 'verify'])
    p.add_argument('--checkout', required=True); p.add_argument('--manifest', required=True)
    p.add_argument('--root', action='append'); p.add_argument('--sha256'); p.add_argument('--copy')
    p.add_argument('--output')
    p = sub.add_parser('verification')
    p.add_argument('action', choices=['run', 'reuse'])
    p.add_argument('--checkout', required=True); p.add_argument('--manifest', required=True)
    p.add_argument('--sha256', required=True); p.add_argument('--toolchain', required=True)
    p.add_argument('--environment', default='{}'); p.add_argument('--output', required=True)
    p.add_argument('--known-scope', action='store_true', help='all dependency inputs are included in manifest roots')
    p.add_argument('--source'); p.add_argument('--copy'); p.add_argument('--heavy', action='store_true')
    p.add_argument('command', nargs='+')
    p = sub.add_parser('manager')
    p.add_argument('action', choices=['start', 'status', 'stop'])
    p.add_argument('--request-id')
    p = sub.add_parser('service')
    p.add_argument('action', choices=['start', 'status', 'stop', 'restart', 'reconcile'])
    p.add_argument('--project', required=True); p.add_argument('--request-id')
    p = sub.add_parser('_service')
    for name in ('project', 'nonce', 'config-root', 'state-root'): p.add_argument('--' + name, required=True)
    p = sub.add_parser('template')
    p.add_argument('action', choices=['list', 'show', 'apply'])
    p.add_argument('name', nargs='?')
    p.add_argument('--request-id')
    p.add_argument('--project', required=True); p.add_argument('--workspace', required=True)
    p = sub.add_parser('recovery')
    p.add_argument('--request-id')
    p.add_argument('action', choices=['status', 'on', 'off'])
    p.add_argument('--project', required=True); p.add_argument('--workspace', required=True)
    for command in ('project','workspace'):
        p = sub.add_parser(command)
        p.add_argument('action', choices=['add','list','update'] + (['disable'] if command=='workspace' else []))
        p.add_argument('--project'); p.add_argument('--workspace'); p.add_argument('--name'); p.add_argument('--path')
        p.add_argument('--orca-id'); p.add_argument('--orca-instance'); p.add_argument('--profile-file'); p.add_argument('--roles-file')
        if command == 'project': p.add_argument('--service-port', type=int)
    for command in ('execute','run','status','operation','goals','command','pause','stop','retry','reconcile','continue-operation','reply','resolve','launch','messages','_runner','prepare-scope'):
        p = sub.add_parser(command)
        p.add_argument('--project', required=command in ('execute', 'prepare-scope')); p.add_argument('--workspace', required=command in ('execute', 'prepare-scope'))
        if command in ('status', 'operation', 'goals', 'command', 'messages', 'pause', 'stop', 'retry', 'reconcile', 'continue-operation', 'reply', 'resolve', 'prepare-scope'):
            p.add_argument('--json', action='store_true')
        if command in ('pause', 'stop', 'retry', 'reconcile', 'continue-operation', 'reply', 'resolve'):
            p.add_argument('--generation', help='observed generation, or null')
            p.add_argument('--attempt', help='observed attempt, or null')
            p.add_argument('--revision')
        if command == 'status':
            p.add_argument('--detail', action='store_true')
            view = p.add_mutually_exclusive_group()
            view.add_argument('--summary', action='store_true')
            view.add_argument('--full', action='store_true')
        if command == 'command':
            p.add_argument('--file', required=True)
        if command == 'operation':
            p.add_argument('operation_action', nargs='?', default='show')
            p.add_argument('--request-id')
            p.add_argument('--until', choices=['operation', 'goal'], default='operation')
            p.add_argument('--timeout', type=float)
        if command == 'execute':
            p.add_argument('--request-id', required=True)
            p.add_argument('--json', action='store_true')
            p.add_argument('--generation'); p.add_argument('--epoch', type=int)
            recovery = p.add_mutually_exclusive_group()
            recovery.add_argument('--recovery', action='store_true', default=None)
            recovery.add_argument('--no-recovery', dest='recovery', action='store_false')
        if command == 'prepare-scope':
            p.add_argument('--request-id', required=True)
            p.add_argument('--generation', required=True)
            p.add_argument('--epoch', type=int, required=True)
            p.add_argument('--items', required=True)
        if command in ('run', 'pause', 'stop', 'retry', 'reconcile', 'continue-operation', 'reply', 'resolve'):
            p.add_argument('--request-id')
        if command in ('run', 'execute'):
            speed = p.add_mutually_exclusive_group()
            speed.add_argument('--fast', dest='fast', action='store_true', default=None)
            speed.add_argument('--no-fast', dest='fast', action='store_false')
            p.add_argument('--goal'); p.add_argument('--resume', action='store_true'); p.add_argument('--items')
            p.add_argument('--mode', choices=['confirm','auto'], default='confirm' if command == 'run' else None)
            if command == 'run':
                p.add_argument('--final', action='store_true')
            else:
                final = p.add_mutually_exclusive_group()
                final.add_argument('--final', action='store_true', default=None)
                final.add_argument('--no-final', dest='final', action='store_false')
            for field in ('config','agent','profile','provider','model','effort','handoff'): p.add_argument('--'+field)
        if command == 'messages':
            p.add_argument('--after')
            p.add_argument('--limit', type=int, default=100)
        if command == '_runner': p.add_argument('--nonce', required=True)
        if command == 'retry':
            p.add_argument('--force', action='store_true'); p.add_argument('--refresh', action='store_true')
            p.add_argument('--allow-extra-attempt', action='store_true', help='authorize one manual attempt beyond the automatic limit')
        if command == 'reply':
            p.add_argument('message_id'); p.add_argument('text', nargs='?'); p.add_argument('--file'); p.add_argument('--run-id')
        if command == 'resolve': p.add_argument('choice', choices=['start','skip','stop'])
        if command == 'continue-operation': p.add_argument('operation_id')
        if command == 'launch':
            from .runner_config import ROLES
            p.add_argument('role', choices=[r for r in ROLES if r != 'coordinator'])
            p.add_argument('--spec', required=True); p.add_argument('--title', required=True)
            p.add_argument('--function'); p.add_argument('--reuse'); p.add_argument('--context-percent', type=float)
            p.add_argument('--keep-session', action='store_true')
            p.add_argument('--variant', choices=['primary', 'reserve'], default='primary')
            p.add_argument('--reason')
    return ap


def _result_exit(result):
    if 'wait_status' in result:
        return {'succeeded': 0, 'blocked': 3, 'timeout': 4}[result['wait_status']]
    return 0 if result.get('status') == 'succeeded' else 3


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    try:
        args = parser(json_errors='--json' in arguments).parse_args(arguments)
    except ContractError as exc:
        print(json.dumps({'schema_version': 1, 'error': exc.as_dict()}, ensure_ascii=False))
        return 2
    catalog = Catalog()
    try:
        if args.cmd in ('manifest', 'verification'):
            from . import verification as verification
            if args.cmd == 'manifest':
                result = (verification.create(args.checkout, args.root, args.manifest) if args.action == 'create'
                          else verification.verify(args.checkout, args.manifest, args.sha256, args.copy))
                if args.output:
                    from .runner_store import atomic_json
                    atomic_json(args.output, result)
            else:
                if not args.known_scope:
                    raise ValueError('unknown dependency scope; verification evidence cannot be reused')
                command = args.command[1:] if args.command[:1] == ['--'] else args.command
                if not command:
                    raise ValueError('verification command required after --')
                environment = json.loads(args.environment)
                attempt = os.environ.get('TEAM_RUNNER_ATTEMPT')
                if args.action == 'run':
                    result = verification.run_check(args.checkout, args.manifest, args.sha256, command,
                        args.toolchain, environment, attempt, args.output, args.heavy, args.copy)
                else:
                    inputs = verification.evidence_inputs(
                        verification.verify(args.checkout, args.manifest, args.sha256, args.copy),
                        command, args.toolchain, environment, True)
                    result = verification.reuse(args.source, inputs, attempt, args.output)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result['result'] == 'PASS' else 1
        if args.cmd == 'manager':
            if os.environ.get('TEAM_RUNNER_ATTEMPT'):
                raise ValueError('child attempt cannot control manager lifecycle')
            from .service_manager import Manager
            import uuid
            manager = Manager(catalog)
            if args.action == 'status':
                result = manager.status()
            else:
                identifier = args.request_id or str(uuid.uuid4())
                result = manager.ensure(identifier) if args.action == 'start' else manager.stop(identifier)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.cmd == '_service':
            from .project_services import run_service
            return run_service(Catalog(args.config_root, args.state_root), args.project, args.nonce) or 0
        if args.cmd == 'service':
            if os.environ.get('TEAM_RUNNER_ATTEMPT'):
                raise ValueError('child attempt cannot control service lifecycle')
            from .project_services import ProjectServices
            import uuid
            manager = ProjectServices(catalog)
            result = (manager.status(args.project) if args.action == 'status' else
                      manager.action(args.project, args.action, args.request_id or str(uuid.uuid4())))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1 if result.get('status') in ('error', 'unknown', 'applying') else 0
        if args.cmd in ('serve','ui'):
            from .runner_ui import serve
            serve(catalog, args.port, args.cmd == 'ui' and not args.no_open)
            return 0
        if args.cmd in ('project','workspace'):
            if args.action == 'list':
                data = catalog.read()['projects']
                if args.cmd == 'workspace':
                    if not args.project: raise ValueError('--project required')
                    data = data[args.project]['workspaces']
            else:
                values = {k: getattr(args,k) for k in ('name','path','orca_id','orca_instance') if getattr(args,k) is not None}
                if args.cmd == 'project' and args.service_port is not None: values['service_port'] = args.service_port
                if args.profile_file: values['profile'] = json.loads(Path(args.profile_file).read_text())
                if args.roles_file: values['roles'] = json.loads(Path(args.roles_file).read_text())
                if args.cmd == 'project':
                    if args.action == 'update' and not args.project: raise ValueError('--project required')
                    data = catalog.project(project_id=args.project if args.action=='update' else None, values=values)
                else:
                    if not args.project: raise ValueError('--project required')
                    if args.action != 'add' and not args.workspace: raise ValueError('--workspace required')
                    if args.action == 'disable': data = catalog.disable(args.project,args.workspace)
                    else: data = catalog.workspace(args.project, values, args.workspace if args.action=='update' else None)
            print(json.dumps(data, ensure_ascii=False, indent=2)); return 0
        runner = selection(args, catalog)
        if args.cmd == 'artifacts':
            from . import artifacts
            if args.action == 'prepare':
                if not args.argv_file or not args.apply:
                    raise ValueError('prepare requires --argv-file and --apply')
                result = artifacts.prepare(runner, args.attempt, json.loads(Path(args.argv_file).read_text()),
                                           reserve_gib=args.reserve_gib, required_gib=args.required_gib,
                                           limit_gib=args.limit_gib)
                print(json.dumps(result, ensure_ascii=False, indent=2)); return 0 if result['exit_code'] == 0 else 1
            elif args.action == 'sweep':
                result = artifacts.sweep(runner, attempt=args.attempt, apply=args.apply)
            elif not args.run:
                raise ValueError('--run required')
            elif args.action == 'restore':
                if not args.tree or not args.apply:
                    raise ValueError('restore requires --tree and --apply')
                result = artifacts.restore(runner, args.run, args.tree)
            else:
                result = artifacts.compact(runner, args.run, apply=args.apply)
            print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
        if args.cmd == 'cleanup':
            from .retention import cleanup
            result = cleanup(runner, apply=args.apply, days=args.days,
                             legacy_root=Path.home() / '.local/state/shm-team/runs' if args.legacy_retired else None)
            print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
        if args.cmd == 'events':
            from .coordinator_events import view, acknowledge
            attempt = os.environ.get('TEAM_RUNNER_ATTEMPT')
            if not attempt:
                raise ValueError('events requires inherited attempt context')
            runner.context.check_attempt(attempt)
            if attempt != (runner.load_state().get('current') or {}).get('attempt_dir'):
                raise ValueError('stale event attempt')
            result = (view(attempt) if args.action == 'list' else
                      acknowledge(runner, attempt, args.dispatch, (args.ids or '').split(',')))
            print(json.dumps(result, ensure_ascii=False)); return 0
        if args.cmd == 'recovery':
            from .recovery import recovery_view
            if args.action == 'status':
                data = recovery_view(runner)
            else:
                from .application import submit_command
                import uuid
                record = submit_command(runner, 'recovery', {'enabled': args.action == 'on'}, args.request_id or str(uuid.uuid4()))
                if record['status'] != 'done':
                    raise ValueError(record.get('error') or 'recovery operation not completed')
                data = record['result']
            print(json.dumps(data, ensure_ascii=False, indent=2)); return 0
        if args.cmd == 'template':
            from . import runner_config as config
            if args.action == 'list':
                if args.name: raise ValueError('template list does not accept a name')
                data = config.templates()
            else:
                if not args.name: raise ValueError('template name required')
                data = config.get_template(args.name)
                if args.action == 'apply':
                    data = config.template_config(args.name, repo=runner.repo,
                                                  config_root=runner.context.config_root.parent)
                    from .application import submit_command
                    import uuid
                    record = submit_command(runner, 'configure', data, args.request_id or str(uuid.uuid4()))
                    if record['status'] != 'done':
                        raise ValueError(record.get('error') or 'configuration operation not completed')
                    data = record['result']
            print(json.dumps(data, ensure_ascii=False, indent=2)); return 0
        if args.cmd == 'command':
            from .application import submit_v1
            result = submit_v1(runner, json.loads(Path(args.file).read_text()))
            print(json.dumps(result, ensure_ascii=False))
            return _result_exit(result) if args.json else (1 if result['status'] in ('failed', 'unknown') else 0)
        if args.cmd == '_runner':
            from .service import run_process
            return run_process(runner, args.nonce)
        if args.cmd == 'prepare-scope':
            from .goal_scope import prepare
            result = prepare(runner, args.items, args.request_id,
                             generation=args.generation, epoch=args.epoch)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.cmd == 'execute':
            from .application import execute
            options = {key: value for key, value in vars(args).items()
                       if key not in ('cmd', 'project', 'workspace', 'request_id', 'json', 'generation', 'epoch', 'resume')
                       and value is not None}
            if args.resume:
                if options:
                    raise ContractError('invalid_payload', 'resume uses saved options only')
                options = {'resume': True}
            result = execute(runner, {'request_id': args.request_id, 'options': options,
                                     'generation': args.generation, 'epoch': args.epoch})
            print(json.dumps(result, ensure_ascii=False))
            return _result_exit(result) if args.json else (0 if result['status'] == 'succeeded' else 1)
        if args.cmd == 'run':
            from .application import submit_command
            import uuid
            data = {'resume':True} if args.resume else {k:v for k,v in vars(args).items() if k not in ('cmd','project','workspace','resume','request_id') and v is not None}
            record = submit_command(runner, 'start', data, args.request_id or str(uuid.uuid4()))
            print(json.dumps(record.get('result', record)))
            return 0 if record['status'] == 'done' else 1
        if args.cmd == 'operation':
            from .application import operation
            action = args.operation_action
            identifier = args.request_id
            if action not in ('show', 'wait'):
                if identifier:
                    raise ContractError('invalid_request', 'Use positional UUID or --request-id, not both')
                identifier, action = action, 'show'
            if not identifier:
                raise ContractError('invalid_request', '--request-id required')
            if action == 'wait':
                from .agent_wait import wait
                data = wait(runner, identifier, until=args.until,
                            timeout=30 if args.timeout is None else args.timeout)
            else:
                if args.until != 'operation' or args.timeout not in (None, 0):
                    raise ContractError('invalid_request', 'show accepts only --until operation and --timeout 0')
                data = operation(runner, identifier)
            print(json.dumps(data, ensure_ascii=False))
            return _result_exit(data) if args.json else 0
        if args.cmd == 'messages' and (args.json or args.after is not None):
            from .agent_messages import messages
            data = messages(runner, after=args.after, limit=args.limit)
            print(json.dumps(data, ensure_ascii=False))
            return 0
        if args.cmd == 'goals' or args.cmd == 'status' and (args.json or args.summary or args.full or args.detail):
            from .application import summary, goals, state_view
            data = ({'schema_version': 1, 'identity': runner.context.identity(), 'goals': goals(runner)}
                    if args.cmd == 'goals' else state_view(runner) if args.full or args.detail else summary(runner))
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0
        if getattr(args, 'json', False) and args.cmd in ('pause', 'stop', 'retry', 'reconcile', 'continue-operation', 'reply', 'resolve'):
            from .application import submit_v1
            if not args.request_id or args.generation is None or args.attempt is None or not args.revision:
                raise ContractError('invalid_scope', '--request-id, --generation, --attempt and --revision required (use null for absent scope)')
            payload = {}
            if args.cmd == 'reply':
                payload = {'message_id': args.message_id, 'text': Path(args.file).read_text() if args.file else args.text}
                if args.run_id is not None:
                    payload['run_id'] = args.run_id
            elif args.cmd == 'resolve':
                payload = {'choice': args.choice}
            elif args.cmd == 'continue-operation':
                payload = {'operation_id': args.operation_id}
            elif args.cmd == 'retry':
                payload = {key: getattr(args, key) for key in ('force', 'refresh', 'allow_extra_attempt')}
            data = submit_v1(runner, {'schema_version': 1, 'request_id': args.request_id,
                                    'action': args.cmd, 'payload': payload,
                                    'scope': {'identity': runner.context.identity(),
                                              'generation': None if args.generation == 'null' else args.generation,
                                              'attempt': None if args.attempt == 'null' else args.attempt,
                                              'revision': args.revision}})
            print(json.dumps(data, ensure_ascii=False))
            return _result_exit(data)
        if args.cmd in ('status','launch','messages'):
            return getattr(runner, 'cmd_'+args.cmd)(args) or 0
        return runner.cmd_control(args) or 0
    except ContractError as exc:
        print(json.dumps({'schema_version': 1, 'error': exc.as_dict()}, ensure_ascii=False))
        return 2 if getattr(args, 'json', False) else 1
    except (ValueError, RuntimeError, OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        if getattr(args, 'json', False) or args.cmd in ('execute', 'command', 'operation', 'goals') or args.cmd == 'status' and args.summary:
            error = ContractError('invalid_request', str(exc))
            print(json.dumps({'schema_version': 1, 'error': error.as_dict()}, ensure_ascii=False))
        else:
            print(str(exc), file=sys.stderr)
        return 2 if getattr(args, 'json', False) else 1
