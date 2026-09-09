"""Independent verifier sessions; no builder capability and no publication API."""
import json
import os
import shutil
import subprocess
from pathlib import Path
from . import review
from .adapter import BASE_ENV_KEYS, cli_version, terminate_process_tree
from .config import expand_command
from .errors import ValidationError
from .budgets import BudgetExhausted, require_remaining
from .models import begin_invocation, finish_invocation
from .util import atomic_write_json, read_json, sha256_file


def schema():
    def obj(properties):
        return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}
    string = {'type': 'string'}
    strings = {'type': 'array', 'items': string}
    return obj({'artifact_sha256': string, 'acceptance_sha256': string, 'verdict': {'type': 'string', 'enum': ['pass','repair']},
        'decision': {'type': 'string', 'enum': ['ACCEPT','REFINE','NEW_CONCEPT','BEST_AVAILABLE']},
        'assessment': string, 'scores': obj({'accuracy': {'type':'integer','minimum':0,'maximum':100}, 'precision': {'type':'integer','minimum':0,'maximum':100}, 'assembly': {'type':'integer','minimum':0,'maximum':100}, 'physical_viability': {'type':'integer','minimum':0,'maximum':100}, 'aesthetic': {'type':'integer','minimum':0,'maximum':100}, 'evidence': {'type':'integer','minimum':0,'maximum':100}}), 'findings': {'type': 'array', 'items': obj({'requirement': string, 'instruction': string,
            'severity': {'type': 'string', 'enum': ['error','warning']}, 'evidence': strings})},
        'resolutions': {'type': 'array', 'items': obj({'task_id': string, 'resolved': {'type': 'boolean'}, 'reason': string, 'evidence': strings})}})


def invoke(root, run_dir, profile, directory, prompt, images=(), *, budget_deadline=None, role='verifier', output_schema=None):
    directory.mkdir(parents=True, exist_ok=True)
    if budget_deadline is not None: require_remaining(budget_deadline)
    frozen = begin_invocation(run_dir, role, cli_version=cli_version(profile))
    result_path = directory / 'result.json'
    schema_path = directory / 'schema.json'
    atomic_write_json(schema_path, output_schema or schema())
    (directory / 'prompt.md').write_text(prompt, encoding='utf-8')
    model = frozen.get('model') or frozen.get('requested_model') or frozen.get('requested', {}).get('model')
    effort = frozen.get('effort') or frozen.get('requested_effort') or frozen.get('requested', {}).get('effort')
    command = expand_command(profile.command, {'python': os.sys.executable, 'root': str(root), 'workspace': str(directory.parent),
        'result_schema': str(schema_path), 'result_file': str(result_path), 'model': model, 'effort': effort,
        'run_dir': str(directory)})
    if images:
        command = command[:-1] + [part for path in images for part in ('--image', str(path.relative_to(directory.parent)))] + command[-1:]
    env = {k: v for k,v in os.environ.items() if k.upper() in BASE_ENV_KEYS | {x.upper() for x in profile.data.get('pass_env', [])}}
    atomic_write_json(directory / 'invocation.json', {'role': role, 'command': command, 'settings': frozen, 'execution_profile_sha256': profile.digest, 'research': profile.data.get('research', {}), 'isolation': 'Profile-controlled sandbox; harness is not an OS container'})
    finished = False
    try:
        if budget_deadline is not None: require_remaining(budget_deadline)
        import time
        with (directory / 'stdout.jsonl').open('w', encoding='utf-8') as stdout, (directory / 'stderr.log').open('w', encoding='utf-8') as stderr:
            process = subprocess.Popen(command, cwd=directory.parent, env=env, stdin=subprocess.PIPE,
                stdout=stdout, stderr=stderr, text=True, encoding='utf-8', errors='replace',
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == 'nt' else 0,
                start_new_session=os.name != 'nt')
            import threading
            def feed():
                try:
                    process.stdin.write(prompt)
                    process.stdin.close()
                except (OSError, ValueError): pass
            feeder = threading.Thread(target=feed, daemon=True)
            feeder.start()
            deadline = time.monotonic() + profile.limit('wall_clock_seconds', 14400)
            if budget_deadline is not None: deadline = min(deadline, budget_deadline)
            try:
                while process.poll() is None:
                    if read_json(run_dir / 'control.json', {}).get('cancel_requested'):
                        raise KeyboardInterrupt('Run cancelled')
                    if time.monotonic() >= deadline:
                        if budget_deadline is not None and deadline == budget_deadline:
                            raise BudgetExhausted('User budget exhausted')
                        raise subprocess.TimeoutExpired(command, profile.limit('wall_clock_seconds', 14400))
                    try: process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired: pass
            except BaseException:
                terminate_process_tree(process)
                raise
            finally:
                feeder.join(timeout=5)
        output = (directory / 'stdout.jsonl').read_text(encoding='utf-8')
        if process.returncode:
            raise ValidationError(f'Verifier failed with exit code {process.returncode}; see {directory}')
        usage = None
        for line in output.splitlines():
            try:
                payload = json.loads(line)
                if 'usage' in payload:
                    usage = payload['usage']
            except ValueError:
                pass
        finish_invocation(run_dir, role, frozen['id'], usage=usage)
        finished = True
        result = read_json(result_path)
        if not isinstance(result, dict):
            raise ValidationError('Verifier did not produce a structured result')
        if output_schema is not None:
            return result
        result.setdefault('decision', 'ACCEPT' if result.get('verdict') == 'pass' else 'REFINE')
        result.setdefault('scores', {'accuracy': 0, 'precision': 0, 'assembly': 0, 'physical_viability': 0, 'aesthetic': 0, 'evidence': 0})
        return result
    except BaseException as exc:
        if not finished: finish_invocation(run_dir, role, frozen['id'], status='failed', error=str(exc))
        raise


def verify(root, run_dir, profile, directory, *, budget_deadline=None):
    artifact = review.integrity(run_dir)
    revision = review.current(run_dir)
    ledger = review.load(run_dir)
    directory.mkdir(parents=True, exist_ok=True)
    inputs = directory / 'inputs'
    if not inputs.exists():
        inputs.mkdir()
        shutil.copy2(artifact, inputs / 'model.blend')
        shutil.copy2(run_dir / 'acceptance.json', inputs / 'acceptance.json')
        shutil.copytree(run_dir / 'snapshot/task', inputs / 'brief')
        for kind in ('measurements', 'diagnostics', 'final'):
            source = run_dir / 'revisions' / revision['id'] / kind
            if source.exists():
                shutil.copytree(source, inputs / kind)
    intro = (f'You are the independent asset verifier. Inspect {inputs}. Artifact SHA256 {revision["sha256"]}; '
        f'acceptance SHA256 {ledger["acceptance_sha256"]}. Establish your own assessment of the brief, evaluated geometry and images. '
        'Read the full brief and every requirement. Inspect required task views and every declared motion sample with image tools. Do not impose animation requirements on a static task. '
        'Assess construction, shape, fidelity, visual defects and whole-asset regression. Concept only, no structural certification. '
        'Measurements labeled measured come from the evaluator; never treat model custom properties as dimensions. '
        'You may create inspection scripts and extra measurements/renders here, labeled agent-authored evidence. '
        'Do not modify model.blend or other supplied evidence. You cannot publish. Never relax requirements/tolerances. '
        'Return actionable instructions with requirement references and relative evidence links for every defect or missing inspection. '
        'Missing required evidence must be reported as unknown and yield repair. A pass/ACCEPT requires all critical interfaces to be geometrically substantiated. '
        'Issue decision ACCEPT for a mature supported candidate, REFINE for local defects, NEW_CONCEPT for a weak architecture/load path/assembly/motion/reference/aesthetic direction, '
        'or BEST_AVAILABLE only when the exploration hard cap is reached. Keep accuracy, precision, assembly, physical viability, aesthetics/brief fit, and evidence quality separate; '
        'aesthetic strength cannot offset a critical mechanical failure. '
        'Evidence arrays must contain plain file paths, optionally followed by #check-id. Never Markdown links or prose. ')
    # Provide authorized evidence directly as well as on disk. Verification remains usable
    # when the agent's shell is unavailable; no sandbox setting is changed.
    briefing = []
    for path in sorted((inputs / 'brief').rglob('*')):
        if path.is_file() and path.suffix in {'.md', '.txt', '.toml'}:
            briefing.append({'path': path.relative_to(directory).as_posix(), 'text': path.read_text(encoding='utf-8-sig')})
    groups = {}
    for check in revision['evaluation']['checks']:
        group = groups.setdefault(check['id'], {k:v for k,v in check.items() if k not in {'frame','actual','deviation','status'}})
        group.setdefault('samples', []).append({k:check.get(k) for k in ('frame','actual','deviation','status')})
    measured = {'source_sha256': revision['sha256'], 'source': 'harness-generated summary of independent Blender evaluator output',
                'coverage': revision['evaluation'].get('coverage'), 'checks': groups}
    atomic_write_json(inputs / 'measurement_summary.json', measured)
    images = []
    image_index = []
    for kind in ('final', 'diagnostics'):
        rendered = read_json(inputs / kind / 'blender_result.json', {})
        for record in rendered.get('renders', []) + rendered.get('motion_renders', []):
            original = Path(record['path'])
            relative = original.relative_to(run_dir / 'revisions' / revision['id'] / kind)
            path = inputs / kind / relative
            if sha256_file(path) != record['sha256']:
                raise ValidationError('Image attachment does not match registered render')
            images.append(path)
            image_index.append({'image': len(images), 'path': path.relative_to(directory).as_posix(),
                                'camera': record.get('camera'), 'frame': record.get('frame'), 'sha256': record['sha256'],
                                'image_quality': {k:record.get(k) for k in ('resolution','decoded','visible_content_coverage','projected_bounds_outside','warnings')}})
    intro += ('\nThe full brief, independent measurements and required images are supplied directly below and as image attachments. '
              'Use these supplied contents if shell tools are unavailable. Do not mistake shell availability for missing evidence. '
              'Hashes are harness-bound evidence identifiers; distinguish them from hashes you compute yourself. '
              'All images in the following ordered index are attached. Assess every requirement and all motion samples.\n' +
              json.dumps({'brief': briefing, 'measurements': measured, 'image_index': image_index}, separators=(',',':')))
    assessment_path = directory / 'assessment.json'
    assessment = read_json(assessment_path)
    if assessment is None:
        assessment = invoke(root, run_dir, profile, directory / 'assessment', intro + 'Do not read builder claims; resolutions must be empty.', images, budget_deadline=budget_deadline)
        review.integrity(run_dir)
        if sha256_file(inputs / 'model.blend') != revision['sha256']:
            raise ValidationError('Verifier changed the inspection artifact')
        atomic_write_json(assessment_path, assessment)
    tasks = ledger['tasks']
    if tasks or any(r['kind'] in {'builder_claim', 'workflow_record'} for r in revision['evidence']):
        claims = {'independent_assessment': assessment, 'tasks': tasks, 'previous_revisions': [{k:r[k] for k in ('id','parent','artifact','sha256')} for r in ledger['revisions'][:-1]], 'builder_claims_directory': str(run_dir / 'evidence'), 'checkpoint_records': str(run_dir / 'checkpoints')}
        atomic_write_json(directory / 'corrections.json', claims)
        result = invoke(root, run_dir, profile, directory / 'correction', intro +
            f'Your independent assessment is already recorded in {assessment_path}. Now compare {directory / "corrections.json"}. '
            'Resolve only builder-addressed tasks whose correction actually works, using matching before/after images and independent measurements. '
            'An ineffective claimed correction remains unresolved. Recheck the entire asset for regressions and create new findings as needed. '
            'Preserve initial findings unless independently disproven. Builder acknowledgement is not resolution. '
            'For every resolved task, its own evidence array must cite at least one current-attempt verifier-authored file '
            'or current measurement/final/diagnostic file. Provided, builder_claim and workflow_record paths alone cannot resolve a task. '
            'For provenance or workflow tasks, write and cite a current independent review of those records.\n' + json.dumps(claims), images, budget_deadline=budget_deadline)
    else:
        result = assessment
    review.integrity(run_dir)
    if sha256_file(inputs / 'model.blend') != revision['sha256']:
        raise ValidationError('Verifier changed the inspection artifact')
    measured_checks = revision['evaluation'].get('checks', [])
    categories = {'accuracy': ('length', 'dimension', 'anchor', 'bounds', 'ground_link', 'input_link', 'coupler_link', 'follower_link'), 'precision': ('material', 'controller', 'feature_'), 'assembly': ('assembly_', 'intersection', 'clearance', 'mating', 'upper_branch'), 'physical_viability': ('intersection', 'clearance', 'mating', 'assembly_'), 'aesthetic': (), 'evidence': ()}
    auditor_scores = dict(result.get('scores', {}))
    result['scores'] = {}
    for category, markers in categories.items():
        if category == 'evidence':
            feature_checks = [check for check in measured_checks if str(check.get('id', '')).startswith('feature_')]
            result['scores'][category] = 100 if all(item.get('evidence') for item in result.get('findings', [])) and all(check.get('status') == 'pass' for check in feature_checks) else 0
        elif category == 'aesthetic':
            result['scores'][category] = int(auditor_scores.get(category, 0))
        else:
            selected = [check for check in measured_checks if any(marker in str(check.get('id', '')) for marker in markers)]
            result['scores'][category] = round(100 * sum(check.get('status') == 'pass' for check in selected) / len(selected)) if selected else 100
    normalize_evidence_links(run_dir, directory, result)
    atomic_write_json(directory / 'verification.json', result)
    return result


def normalize_evidence_links(run_dir, directory, result):
    """Own mutable claim references without promoting them to independent evidence."""
    ledger = review.load(run_dir)
    registered = {r['path']: r for rev in ledger['revisions'] for r in rev['evidence']}
    for item in result.get('findings', []) + result.get('resolutions', []):
        links = []
        for link in item.get('evidence', []):
            raw, separator, fragment = link.partition('#')
            candidates = [directory / raw, directory / 'correction' / raw, directory / 'assessment' / raw, run_dir / raw]
            path = next((p.resolve() for p in candidates if p.is_file()), None)
            if path is None:
                raise ValidationError('Verifier evidence link does not exist: ' + link)
            try:
                relative = path.relative_to(run_dir.resolve()).as_posix()
            except ValueError:
                raise ValidationError('Verifier evidence escapes owned run')
            if relative.startswith('workspace/'):
                digest = sha256_file(path)
                owned = next((r for r in registered.values() if r['kind'] in {'builder_claim','provided'} and r['sha256'] == digest), None)
                if owned:
                    path = run_dir / owned['path']
                    relative = owned['path']
            if relative not in registered and not path.is_relative_to(directory.resolve()):
                target = directory / 'claim_snapshots' / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and sha256_file(target) != sha256_file(path):
                    raise ValidationError('Owned claim snapshot changed')
                if not target.exists(): shutil.copy2(path, target)
                relative = target.relative_to(run_dir).as_posix()
            links.append(relative + (separator + fragment if separator else ''))
        item['evidence'] = links
    return result


def recover_completed_verification(run_dir):
    """Resume a completed assessment whose evidence-link handoff was interrupted."""
    from .util import utc_now
    revision = review.current(run_dir)
    if revision.get('verification') is not None: return True
    directory = run_dir / 'revisions' / revision['id'] / 'verification'
    for attempt in sorted(directory.glob('attempt_*'), reverse=True):
        completed = attempt / 'verification.json'
        if not completed.is_file() or (attempt / 'recovery.json').exists(): continue
        original_hash = sha256_file(completed)
        result = read_json(completed)
        try:
            normalize_evidence_links(run_dir, attempt, result)
            atomic_write_json(attempt / 'recovery.json', {'at': utc_now(), 'source_sha256': original_hash, 'status': 'recovering', 'reason': 'Reuse completed assessment; normalize evidence ownership only'})
            atomic_write_json(completed, result)
            review.record_evidence(run_dir, directory, 'verification')
            review.record_verification(run_dir, result)
            review.stage(run_dir, 'verification', 'done', recovered_from=attempt.relative_to(run_dir).as_posix())
            return True
        except ValidationError as exc:
            atomic_write_json(attempt / 'recovery.json', {'at': utc_now(), 'source_sha256': original_hash, 'status': 'rejected', 'error': str(exc)})
    return False
