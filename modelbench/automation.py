"""Resumable orchestration; quality failures have no automatic cycle cap."""
import copy
import shutil
import time
from pathlib import Path
from . import review, models
from .adapter import run_adapter, AgentResult
from .budgets import BudgetExhausted, require_remaining
from .blender import run_blender, STANDARD_VIEWS
from .config import AgentProfile, load_agent_profile, load_toml
from .errors import StateError
from .state import load_state, save_state, transition
from .util import atomic_write_json, read_json, sha256_file, utc_now


def configure(root, run_dir, builder, verifier_name, overrides=None, budget_seconds=None):
    verifier = load_agent_profile(root, verifier_name)
    def model_profile(profile, role):
        value = profile.data.get('models', {}).get(role)
        if value is None:
            # Explicit harness defaults for older command profiles; never global Codex settings.
            value = {'model': 'gpt-6-astra', 'effort': 'high', 'allowed_models': {'gpt-6-astra': ['medium','high']}}
        return value
    models.initialize(run_dir, model_profile(builder, 'builder'), model_profile(verifier, 'verifier'), overrides or {})
    shutil.copy2(verifier.path, run_dir / 'snapshot/verifier_profile.toml')
    atomic_write_json(run_dir / 'automation.json', {'version': 1, 'verifier': verifier.name,
        'verifier_profile_sha256': verifier.digest, 'budget_seconds': budget_seconds, 'created_at': utc_now()})
    review.initialize(run_dir)


def profile(run_dir, role):
    metadata = read_json(run_dir / 'run.json')
    if role == 'builder':
        path = run_dir / 'snapshot/agent_profile.toml'
        return AgentProfile(metadata['agent_profile']['name'], path, load_toml(path), metadata['agent_profile']['sha256'])
    config = read_json(run_dir / 'automation.json')
    path = run_dir / 'snapshot/verifier_profile.toml'
    if sha256_file(path) != config['verifier_profile_sha256']:
        raise StateError('Verifier profile snapshot changed')
    return AgentProfile(config['verifier'], path, load_toml(path), config['verifier_profile_sha256'])


def reset_retries(run_dir):
    with review.lock(run_dir):
        value = review.load(run_dir)
        for key, operation in value.get('retries', {}).items():
            if operation['status'] == 'incomplete':
                review.event(value, 'retry_resumed', operation=key, previous_attempts=operation['attempts'])
                operation['attempts'] = 0
                operation['status'] = 'pending'
        review.save(run_dir, value)


def retry(run_dir, stage, operation):
    with review.lock(run_dir):
        value = review.load(run_dir)
        revision_id = value['revisions'][-1]['id'] if value['revisions'] else 'initial'
        key = revision_id + ':' + stage
        record = value.setdefault('retries', {}).setdefault(key, {'attempts': 0, 'status': 'pending'})
        if record['status'] == 'done': record = value['retries'][key] = {'attempts': 0, 'status': 'pending'}
        first_attempt = record['attempts']
        review.save(run_dir, value)
        if first_attempt >= 3:
            raise StateError('Operation retries exhausted; use resume: ' + key)
    for attempt in range(first_attempt, 3):
        with review.lock(run_dir):
            value = review.load(run_dir)
            value['retries'][key] = {'attempts': attempt + 1, 'status': 'running'}
            review.save(run_dir, value)
        review.stage(run_dir, stage, 'running', attempt=attempt + 1)
        try:
            result = operation(attempt)
            with review.lock(run_dir):
                value = review.load(run_dir)
                value['retries'][key]['status'] = 'done'
                review.save(run_dir, value)
            review.stage(run_dir, stage, 'done', attempt=attempt + 1)
            return result
        except BaseException as exc:
            with review.lock(run_dir):
                value = review.load(run_dir)
                value['retries'][key]['status'] = 'incomplete'
                review.save(run_dir, value)
            review.stage(run_dir, stage, 'incomplete', attempt=attempt + 1, error=str(exc))
            if isinstance(exc, (KeyboardInterrupt, SystemExit, StateError)) or attempt == 2:
                raise


def controlled(run_dir):
    control = read_json(run_dir / 'control.json', {})
    if control.get('cancel_requested'):
        transition(run_dir, 'cancelled', force=True)
        return True
    if control.get('pause_requested'):
        transition(run_dir, 'awaiting_feedback', force=True)
        return True
    return False


DIAGNOSTIC_VIEWS = ('front', 'right', 'top', 'azimuth_045')


def _expected_render_paths(run_dir, kind):
    task = read_json(run_dir / 'acceptance.json', {}).get('task', {})
    views = STANDARD_VIEWS if kind == 'final' else DIAGNOSTIC_VIEWS
    standard = {f"{'orthographic' if view in STANDARD_VIEWS[:6] else 'turntable'}/{view}.png" for view in views}
    frames = [] if kind == 'final' else task.get('verification', {}).get('frames', [])
    return standard, {f'motion/front_{int(frame):04d}.png' for frame in frames}


def _canonical_render_result(run_dir, directory, result, kind):
    if not isinstance(result, dict):
        raise StateError('Missing render result')
    value = copy.deepcopy(result)
    standard, motion = _expected_render_paths(run_dir, kind)
    for key, expected in (('renders', standard), ('motion_renders', motion)):
        records = value.get(key, [])
        if not isinstance(records, list):
            raise StateError(f'Render result is missing {key} records')
        rebound = []
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get('path'), str):
                raise StateError(f'Render result contains an invalid {key} record')
            raw = record['path'].replace(chr(92), '/')
            matches = [relative for relative in expected if raw == relative or raw.endswith('/' + relative)]
            if len(matches) != 1:
                raise StateError(f'Render result has an unexpected {key} path')
            rebound.append({**record, 'path': str((directory / matches[0]).resolve())})
        value[key] = rebound
    return value


def validate_render_set(run_dir, directory, result, kind, artifact_sha256):
    if kind not in {'final', 'diagnostics'}:
        raise StateError('Unknown review render kind')
    if not isinstance(result, dict) or result.get('ok') is not True or result.get('source_sha256') != artifact_sha256:
        raise StateError('Render result is not bound to the submitted artifact')
    standard, motion = _expected_render_paths(run_dir, kind)
    root = directory.resolve()
    recorded = set()
    for key, expected in (('renders', standard), ('motion_renders', motion)):
        records = result.get(key, [])
        if not isinstance(records, list):
            raise StateError(f'Render result is missing {key} records')
        paths = set()
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get('path'), str):
                raise StateError(f'Render result contains an invalid {key} record')
            candidate = Path(record['path'])
            path = candidate.resolve()
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError as exc:
                raise StateError(f'Render result {key} path escapes its stage directory') from exc
            if relative in recorded:
                raise StateError(f'Duplicate render record: {relative}')
            if candidate.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
                raise StateError(f'Render output is missing, linked, or empty: {relative}')
            if record.get('size') != path.stat().st_size or record.get('sha256') != sha256_file(path):
                raise StateError(f'Render output does not match its recorded hash: {relative}')
            paths.add(relative); recorded.add(relative)
        if paths != expected:
            if kind == 'final' and key == 'renders':
                raise StateError('Final evaluation must contain exactly the 14 standardized PNG views')
            raise StateError(f'Render result does not contain the exact required {key} set')
    if result.get('diagnostic_renders', []) not in (None, []):
        raise StateError('Unexpected diagnostic-camera records in standardized evidence')
    actual = {p.relative_to(directory).as_posix() for p in directory.rglob('*.png') if p.is_file()}
    if actual != standard | motion:
        raise StateError('Render directory contains a missing or unexpected PNG')
    return {relative: sha256_file(directory / relative) for relative in actual}


def render_stage(root, run_dir, revision, kind, *, budget_deadline=None):
    directory = run_dir / 'revisions' / revision['id'] / kind
    cached = read_json(directory / 'blender_result.json')
    if cached:
        repaired = _canonical_render_result(run_dir, directory, cached, kind)
        validate_render_set(run_dir, directory, repaired, kind, revision['sha256'])
        if repaired != cached:
            atomic_write_json(directory / 'blender_result.json', repaired)
        return repaired
    metadata = read_json(run_dir / 'run.json')
    task = read_json(run_dir / 'snapshot/task.json')
    render_profile = load_toml(run_dir / ('snapshot/final_profile.toml' if kind == 'final' else 'snapshot/checkpoint_profile.toml'))
    def operation(attempt):
        attempts = list(directory.parent.glob('.' + kind + '-attempt-*'))
        target = directory.parent / f'.{kind}-attempt-{len(attempts)+1:04d}'
        result = run_blender(root, run_dir, review.integrity(run_dir), target, task, render_profile,
            mode='final' if kind == 'final' else 'checkpoint',
            timeout=metadata['agent_profile']['data'].get('limits', {}).get('blender_seconds', 3600),
            views=None if kind == 'final' else ['front','right','top','azimuth_045'],
            motion_frames=[] if kind == 'final' else task.get('verification', {}).get('frames', []),
            **({'budget_deadline': budget_deadline} if budget_deadline is not None else {}))
        result = _canonical_render_result(run_dir, target, result, kind)
        validate_render_set(run_dir, target, result, kind, revision['sha256'])
        if kind == 'final':
            from .orchestrator import _validate_render_result
            _validate_render_result(target, result, revision['sha256'])
        review.integrity(run_dir)
        result = _canonical_render_result(run_dir, directory, result, kind)
        atomic_write_json(target / 'blender_result.json', result)
        target.rename(directory)
        return result
    return retry(run_dir, kind, operation)


def run_loop(root, generated, run_dir):
    from .measurements import evaluate
    from .verifier import verify, recover_completed_verification
    from .orchestrator import _owned_submission
    from .runs import verify_run_snapshot
    from .orchestrator import promote
    started = time.monotonic()
    previously_used = review.load(run_dir).get('budget_used_seconds', 0)
    budget = read_json(run_dir / 'automation.json', {}).get('budget_seconds')
    grants = review.load(run_dir).get('budget_grants', [])
    if budget is not None:
        budget = float(budget) + sum(float(grant['seconds']) for grant in grants if isinstance(grant, dict) and isinstance(grant.get('seconds'), (int, float)))
    budget_deadline = None if budget is None else started + max(0.0, budget - float(previously_used))
    def check_budget():
        require_remaining(budget_deadline)
    try:
        while True:
            verify_run_snapshot(run_dir)
            if controlled(run_dir):
                break
            check_budget()
            value = review.load(run_dir)
            owned = load_state(run_dir).get('artifact')
            if owned and (not value['revisions'] or value['revisions'][-1]['artifact'] != owned['path']):
                from .util import resolve_within
                artifact = resolve_within(run_dir, owned['path'])
                if sha256_file(artifact) != owned['sha256']: raise StateError('Pending artifact changed')
                review.register_revision(run_dir, artifact)
                review.stage(run_dir, 'measurements')
                value = review.load(run_dir)
            pending_builder = not value['revisions'] or value['operation']['stage'] == 'builder'
            if pending_builder:
                transition(run_dir, 'modeling', force=True)
                result = retry(run_dir, 'builder', lambda _: run_adapter(root, run_dir, profile(run_dir, 'builder'), **({'budget_deadline': budget_deadline} if budget_deadline is not None else {})))
                if result.status == 'failed':
                    review.stage(run_dir, 'builder', 'incomplete', error=result.notes or 'Builder reported failure')
                    raise StateError(result.notes or 'Builder reported failure')
                if result.status == 'checkpoint':
                    from .checkpoints import create_checkpoint
                    from .util import resolve_within
                    state = load_state(run_dir)
                    source = resolve_within(run_dir / 'workspace', result.blend_path)
                    latest = state.get('latest_checkpoint')
                    if not latest or sha256_file(run_dir / 'checkpoints' / latest / 'model.blend') != sha256_file(source):
                        check_budget()
                        create_checkpoint(root, run_dir, source, result.phase, **({'budget_deadline': budget_deadline} if budget_deadline is not None else {}))
                    review.stage(run_dir, 'builder')
                    if controlled(run_dir): break
                    continue
                artifact = _owned_submission(run_dir, result)
                review.register_revision(run_dir, artifact, builder_invocation=result.invocation)
                transition(run_dir, 'submitted', force=True)
            revision = review.current(run_dir)
            directory = run_dir / 'revisions' / revision['id']
            if not revision['evaluation'] or not revision.get('evaluation_receipt'):
                transition(run_dir, 'validating', force=True)
                result = retry(run_dir, 'measurements', lambda _: evaluate(root, run_dir, review.integrity(run_dir), directory / 'measurements', **({'budget_deadline': budget_deadline} if budget_deadline is not None else {})))
                review.record_evaluation(run_dir, result)
            review.record_supporting_evidence(run_dir)
            review.record_evidence(run_dir, directory / 'measurements', 'measurements')
            check_budget()
            render_stage(root, run_dir, revision, 'diagnostics', budget_deadline=budget_deadline)
            review.record_evidence(run_dir, directory / 'diagnostics', 'diagnostics')
            review.comparisons(run_dir)
            if review.current(run_dir)['evaluation'].get('passed') is True:
                check_budget()
                render_stage(root, run_dir, revision, 'final', budget_deadline=budget_deadline)
                review.record_evidence(run_dir, directory / 'final', 'final')
            if controlled(run_dir): break
            revision = review.current(run_dir)
            if revision['verification'] is None and not recover_completed_verification(run_dir):
                transition(run_dir, 'verifying', force=True)
                def verify_attempt(attempt):
                    attempt_dir = directory / 'verification' / f'attempt_{len(list((directory / "verification").glob("attempt_*")))+1:04d}'
                    result = verify(root, run_dir, profile(run_dir, 'verifier'), attempt_dir, **({'budget_deadline': budget_deadline} if budget_deadline is not None else {}))
                    review.record_evidence(run_dir, directory / 'verification', 'verification')
                    review.record_verification(run_dir, result)
                    return result
                retry(run_dir, 'verification', verify_attempt)
            value = review.load(run_dir)
            revision = value['revisions'][-1]
            exploration = value.get('exploration', {})
            if exploration.get('enabled') and exploration.get('finished') and exploration.get('outcome') == 'BEST_AVAILABLE':
                transition(run_dir, 'completed', detail={'decision': 'BEST_AVAILABLE', 'selected_concept_id': exploration.get('selected_concept_id')}, force=True)
                state = load_state(run_dir)
                return {'run': f"{state['task_id']}/{state['run_id']}", 'state': 'completed', 'decision': 'BEST_AVAILABLE',
                        'selected_concept_id': exploration.get('selected_concept_id'), 'unresolved_findings': [t['id'] for t in value['tasks'] if t['status'] != 'verifier_resolved']}
            if revision['evaluation'].get('passed') and revision['verification']['verdict'] == 'pass' and revision['verification'].get('decision', 'ACCEPT') == 'ACCEPT' and all(t['status'] == 'verifier_resolved' for t in value['tasks']):
                if controlled(run_dir): break
                check_budget()
                with review.lock(run_dir):
                    check_budget()
                    review.assert_publishable(run_dir, revision['sha256'])
                    review.stage(run_dir, 'publication', 'running')
                    source_final = directory / 'final'
                    source_result = _canonical_render_result(run_dir, source_final, read_json(source_final / 'blender_result.json'), 'final')
                    source_hashes = validate_render_set(run_dir, source_final, source_result, 'final', revision['sha256'])
                    final = run_dir / 'renders/final'
                    same = False
                    if final.exists():
                        try:
                            mirror_result = _canonical_render_result(run_dir, final, read_json(final / 'blender_result.json'), 'final')
                            same = validate_render_set(run_dir, final, mirror_result, 'final', revision['sha256']) == source_hashes
                            if same: atomic_write_json(final / 'blender_result.json', mirror_result)
                        except (OSError, StateError, TypeError, ValueError):
                            same = False
                    if not same:
                        import uuid
                        staging = run_dir / 'renders' / ('.final-mirror-attempt-' + uuid.uuid4().hex)
                        shutil.copytree(source_final, staging)
                        staged_result = _canonical_render_result(run_dir, staging, source_result, 'final')
                        if validate_render_set(run_dir, staging, staged_result, 'final', revision['sha256']) != source_hashes:
                            raise StateError('Publication render mirror differs from registered final evidence')
                        atomic_write_json(staging / 'blender_result.json', _canonical_render_result(run_dir, final, staged_result, 'final'))
                        if final.exists():
                            archive = run_dir / 'renders' / ('previous-final-' + uuid.uuid4().hex)
                            final.rename(archive)
                        staging.rename(final)
                    publication = retry(run_dir, 'publication', lambda _: promote(generated, run_dir, review.integrity(run_dir), expected_artifact_sha256=revision['sha256'], **({'budget_deadline': budget_deadline} if budget_deadline is not None else {})))
                    state = load_state(run_dir)
                    state['publication'], state['failure'] = publication, None
                    save_state(run_dir, state)
                    transition(run_dir, 'promoted', force=True)
                    return {'run': f"{state['task_id']}/{state['run_id']}", 'state': 'promoted', 'publication': publication}
            # Preserve all before renders. The next revision uses identical task cameras/settings.
            if exploration.get('enabled') and exploration.get('phase') == 'refine' and exploration.get('selected_concept_id'):
                selected = next((item for item in reversed(value['revisions']) if (item.get('design_iteration') or {}).get('concept_id') == exploration['selected_concept_id']), None)
                if selected is not None:
                    shutil.copy2(run_dir / selected['artifact'], run_dir / 'workspace' / 'seed.blend')
            review.stage(run_dir, 'builder')
    except BudgetExhausted:
        review.stage(run_dir, 'budget', 'incomplete', error='User budget exhausted')
        transition(run_dir, 'interrupted', force=True)
    except BaseException as exc:
        state = load_state(run_dir)
        state['failure'] = {'stage': 'automation', 'message': str(exc), 'at': utc_now()}
        save_state(run_dir, state)
        transition(run_dir, 'cancelled' if read_json(run_dir / 'control.json', {}).get('cancel_requested') else 'interrupted', force=True)
        raise
    finally:
        with review.lock(run_dir):
            value = review.load(run_dir)
            value['budget_used_seconds'] = previously_used + time.monotonic() - started
            review.save(run_dir, value)
    state = load_state(run_dir)
    return {'run': f"{state['task_id']}/{state['run_id']}", 'state': state['state']}
