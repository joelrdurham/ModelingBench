"""Durable acceptance, revisions, corrections and publication gate."""
from pathlib import Path
import shutil
from .errors import StateError, ValidationError
from .util import atomic_write_json, canonical_hash, file_inventory, read_json, resolve_within, sha256_file, utc_now


def lock(run_dir):
    from .models import mutation_lock
    return mutation_lock(run_dir)


def load(run_dir):
    value = read_json(run_dir / 'review.json')
    if not isinstance(value, dict):
        raise StateError('Independent verification Not recorded; create a revision run')
    return value


def save(run_dir, value):
    value['generation'] += 1
    value['updated_at'] = utc_now()
    atomic_write_json(run_dir / 'review.json', value)


def event(value, kind, **data):
    value['events'].append({'at': utc_now(), 'type': kind, **data})


def initialize(run_dir):
    task = read_json(run_dir / 'snapshot/task.json')
    from .design import ExplorationState, parse_design_manifest
    discovered = task.get('specification_mode') == 'discovered'
    manifest = None if discovered else parse_design_manifest(task)
    exploration_config = task.get('design_exploration', {}) if isinstance(task, dict) else {}
    enabled = not discovered and isinstance(exploration_config, dict) and exploration_config.get('enabled') is True
    exploration = ExplorationState(enabled=enabled,
        concept_limit=int(exploration_config.get('concepts', 3)) if enabled else 3,
        refinement_limit=int(exploration_config.get('refinement_rounds', 4)) if enabled else 4,
        cycle_limit=int(exploration_config.get('cycles', 2)) if enabled else 2)
    checklist = {'version': 1, 'brief': file_inventory(run_dir / 'snapshot/task'),
                 'task': task, 'design_manifest': manifest.record() if manifest else None,
                 'policy': 'All snapshotted brief requirements remain binding. Unspecified measurements are unassessed. No tolerance relaxation.'}
    with lock(run_dir):
        if (run_dir / 'review.json').exists():
            raise StateError('Review already initialized')
        atomic_write_json(run_dir / 'acceptance.json', checklist)
        atomic_write_json(run_dir / 'review.json', {'version': 1, 'generation': 0, 'acceptance_sha256': sha256_file(run_dir / 'acceptance.json'),
            'revisions': [], 'tasks': [], 'events': [], 'operation': {'stage': 'builder', 'status': 'pending'},
            'exploration': {**exploration.record(), 'audits': []}})


def stage(run_dir, name, status='pending', **details):
    with lock(run_dir):
        value = load(run_dir)
        value['operation'] = {'stage': name, 'status': status, **details}
        event(value, 'operation', **value['operation'])
        save(run_dir, value)


def current(run_dir):
    revisions = load(run_dir)['revisions']
    if not revisions:
        raise StateError('No submitted revision')
    return revisions[-1]


def register_revision(run_dir, artifact, builder_invocation=None):
    with lock(run_dir):
        value = load(run_dir)
        exploration = value.get('exploration', {})
        revision = {'id': f"revision_{len(value['revisions']) + 1:06d}", 'parent': value['revisions'][-1]['id'] if value['revisions'] else None,
                    'artifact': artifact.relative_to(run_dir).as_posix(), 'sha256': sha256_file(artifact), 'at': utc_now(),
                    'evidence': supporting_records(run_dir), 'evaluation': None, 'verification': None,
                    'design_iteration': ({'concept_id': exploration.get('current_concept_id'), 'cycle': exploration.get('cycles'),
                        'phase': exploration.get('phase'), 'refinement_round': exploration.get('refinements')} if exploration.get('enabled') else None),
                    'builder_provenance': ({**builder_invocation, 'artifact_sha256': sha256_file(artifact)} if isinstance(builder_invocation, dict) else None)}
        from .discovery import enabled as discovery_enabled, active_record
        if discovery_enabled(run_dir):
            revision['specification_sha256'] = active_record(run_dir)['specification']['sha256']
        directory = run_dir / 'revisions' / revision['id']
        if directory.exists():
            receipt = read_json(directory / 'revision.json')
            if receipt and receipt['artifact'] == revision['artifact'] and receipt['sha256'] == revision['sha256']:
                value['revisions'].append(receipt)
                value['operation'] = {'stage': 'measurements', 'status': 'pending'}
                event(value, 'operation', **value['operation'])
                save(run_dir, value)
                return receipt
            import uuid
            directory.rename(directory.with_name('.incomplete-' + directory.name + '-' + uuid.uuid4().hex))
        directory.mkdir(parents=True)
        for source in sorted((run_dir / 'workspace').rglob('*.py')):
            if not source.is_symlink():
                target = directory / 'scripts' / source.relative_to(run_dir / 'workspace')
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        atomic_write_json(directory / 'revision.json', revision)
        value['revisions'].append(revision)
        event(value, 'submitted', revision=revision['id'], sha256=revision['sha256'])
        value['operation'] = {'stage': 'measurements', 'status': 'pending'}
        event(value, 'operation', **value['operation'])
        save(run_dir, value)
        return revision


def integrity(run_dir, revision=None):
    value = load(run_dir)
    if sha256_file(run_dir / 'acceptance.json') != value['acceptance_sha256']:
        raise StateError('Acceptance checklist changed')
    revision = revision or current(run_dir)
    artifact = resolve_within(run_dir, revision['artifact'])
    if sha256_file(artifact) != revision['sha256']:
        raise StateError('Owned artifact changed')
    return artifact


def add_task(run_dir, instruction, *, requirement='human-feedback', source='human', severity='error', evidence=None, task_id=None):
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValidationError('Task instruction must be nonempty')
    with lock(run_dir):
        from .state import load_state
        if load_state(run_dir)['state'] in {'completed', 'promoted', 'cancelled'}:
            raise StateError('Run is immutable; create a revision')
        value = load(run_dir)
        stable_id = task_id or ('task_' + canonical_hash([source, requirement, instruction])[:16])
        task = next((t for t in value['tasks'] if t['id'] == stable_id), None)
        if task:
            for item in evidence or []:
                if item not in task['evidence']: task['evidence'].append(item)
            if task['status'] == 'verifier_resolved':
                task['status'] = 'open'
                task['history'].append({'at': utc_now(), 'type': 'reopened', 'evidence': evidence or []})
        else:
            task = {'id': stable_id, 'requirement': requirement, 'instruction': instruction.strip(), 'source': source,
                    'severity': severity, 'status': 'open', 'evidence': evidence or [], 'responses': [],
                    'history': [{'at': utc_now(), 'type': 'opened'}]}
            value['tasks'].append(task)
        event(value, 'task', id=stable_id, status=task['status'])
        save(run_dir, value)
        return task


def respond(run_dir, task_id, response):
    if not isinstance(response, str) or not response.strip():
        raise ValidationError('A task-specific builder response is required')
    with lock(run_dir):
        value = load(run_dir)
        task = next((t for t in value['tasks'] if t['id'] == task_id), None)
        if task is None or task['status'] == 'verifier_resolved':
            raise StateError('Task is missing or already resolved')
        task['status'] = 'builder_addressed'
        task['responses'].append({'at': utc_now(), 'response': response, 'after_revision': value['revisions'][-1]['id'] if value['revisions'] else None})
        task['history'].append({'at': utc_now(), 'type': 'builder_addressed'})
        save(run_dir, value)
        return task


def supporting_records(run_dir):
    from .evidence import evidence_records
    records = []
    for supplied in evidence_records(run_dir):
        path = resolve_within(run_dir, supplied['local_path'])
        if sha256_file(path) != supplied['sha256']:
            raise StateError('Registered supporting evidence changed: ' + supplied['local_path'])
        records.append({'path': supplied['local_path'], 'sha256': supplied['sha256'], 'size': supplied['size'],
                        'kind': 'provided' if supplied['origin'] == 'provided' else 'builder_claim',
                        'source': supplied['origin'], 'title': supplied.get('title')})
    for folder in sorted((run_dir / 'checkpoints').glob('checkpoint_*')):
        if not folder.is_dir() or folder.is_symlink(): continue
        records.extend({'path': (folder / item['path']).relative_to(run_dir).as_posix(), 'sha256': item['sha256'],
                        'size': item['size'], 'kind': 'workflow_record'} for item in file_inventory(folder))
    return records


def record_supporting_evidence(run_dir):
    with lock(run_dir):
        value = load(run_dir)
        revision = value['revisions'][-1]
        revision['evidence'] = [r for r in revision['evidence'] if r['kind'] not in {'provided','workflow_record'} and not (r['kind'] == 'builder_claim' and r['path'].startswith('evidence/files/'))] + supporting_records(run_dir)
        save(run_dir, value)


def record_evidence(run_dir, directory, kind):
    with lock(run_dir):
        value = load(run_dir)
        records = [{'path': (directory / r['path']).relative_to(run_dir).as_posix(), 'sha256': r['sha256'], 'size': r['size'], 'kind': 'builder_claim' if kind == 'verification' and 'claim_snapshots' in (directory / r['path']).parts else kind} for r in file_inventory(directory)]
        revision = value['revisions'][-1]
        revision['evidence'] = [r for r in revision['evidence'] if r['kind'] != kind and not r['path'].startswith(directory.relative_to(run_dir).as_posix() + '/')] + records
        save(run_dir, value)
        return records


def record_evaluation(run_dir, result):
    with lock(run_dir):
        value = load(run_dir)
        revision = value['revisions'][-1]
        if result.get('source_sha256') != revision['sha256'] or result.get('ok') is not True:
            raise StateError('Evaluation is not bound to the submitted artifact')
        result_path = run_dir / 'revisions' / revision['id'] / 'measurements' / 'assessment.json'
        atomic_write_json(result_path, result)
        revision['evaluation'] = result
        revision['evaluation_receipt'] = {'path': result_path.relative_to(run_dir).as_posix(), 'sha256': sha256_file(result_path)}
        records = [{'path': (result_path.parent / r['path']).relative_to(run_dir).as_posix(), 'sha256': r['sha256'], 'size': r['size'], 'kind': 'measurements'} for r in file_inventory(result_path.parent)]
        revision['evidence'] = [r for r in revision['evidence'] if r['kind'] != 'measurements'] + records
        save(run_dir, value)
    for finding in result.get('checks', []):
        if finding.get('status') in {'fail', 'unknown'}:
            add_task(run_dir, f"Correct {finding.get('requirement', finding['id'])}: expected {finding.get('expected')}, measured {finding.get('actual')}, tolerance {finding.get('tolerance')}, frame {finding.get('frame')}.",
                     requirement=finding.get('requirement', finding['id']), source='measured',
                     evidence=[finding], task_id='measured_' + finding['id'])


def record_verification(run_dir, result):
    with lock(run_dir):
        value = load(run_dir)
        revision = value['revisions'][-1]
        integrity(run_dir, revision)
        if result.get('artifact_sha256') != revision['sha256'] or result.get('acceptance_sha256') != value['acceptance_sha256']:
            raise ValidationError('Verifier assessed the wrong artifact or checklist')
        if result.get('verdict') not in {'pass', 'repair'} or not isinstance(result.get('assessment'), str) or not result['assessment'].strip():
            raise ValidationError('Malformed verifier verdict')
        decision = result.get('decision', 'ACCEPT' if result.get('verdict') == 'pass' else 'REFINE')
        if decision not in {'ACCEPT', 'REFINE', 'NEW_CONCEPT', 'BEST_AVAILABLE'}:
            raise ValidationError('Malformed design audit decision')
        if (decision == 'ACCEPT') != (result['verdict'] == 'pass'):
            raise ValidationError('Only an ACCEPT decision may carry a pass verdict')
        result['decision'] = decision
        raw_scores = result.get('scores', {})
        if not isinstance(raw_scores, dict):
            raise ValidationError('Verifier scores must be an object')
        for axis in ('accuracy', 'precision', 'assembly', 'physical_viability', 'aesthetic', 'evidence'):
            score = raw_scores.setdefault(axis, 0)
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 100:
                raise ValidationError('Verifier axis scores must be between 0 and 100')
        if not isinstance(result.get('findings'), list) or not isinstance(result.get('resolutions'), list):
            raise ValidationError('Verifier findings and resolutions must be arrays')
        if result['verdict'] == 'pass' and result['findings']:
            raise ValidationError('A pass cannot contain actionable findings')
        from .discovery import verify_requirements, retain_candidates
        verify_requirements(run_dir, revision, result)
        decisions = []
        for decision in result['resolutions']:
            task = next((t for t in value['tasks'] if t['id'] == decision.get('task_id')), None)
            if task is None or not isinstance(decision.get('resolved'), bool) or not decision.get('reason'):
                raise ValidationError('Invalid correction decision')
            if decision['resolved'] and (task['status'] != 'builder_addressed' or not decision.get('evidence')):
                raise ValidationError('Resolution requires builder response and independent evidence')
            if decision['resolved'] and task['source'] == 'measured':
                check_id = task['id'].removeprefix('measured_')
                checks = (revision.get('evaluation') or {}).get('checks', [])
                matching = [c for c in checks if c['id'] == check_id]
                if not matching or any(c['status'] != 'pass' for c in matching):
                    decision = {**decision, 'resolved': False, 'reason': 'Independent measurements still fail: ' + decision['reason']}
            if decision['resolved']:
                for link in decision['evidence']:
                    if not isinstance(link, str): raise ValidationError('Evidence links must be relative paths')
                    path = resolve_within(run_dir, link.split('#', 1)[0])
                    if not path.is_file(): raise ValidationError('Resolution evidence missing')
                    relative = path.relative_to(run_dir.resolve()).as_posix()
                    evidence = [r for rev in value['revisions'] for r in rev['evidence'] if r['path'] == relative]
                    if not evidence or sha256_file(path) != evidence[0]['sha256']:
                        raise ValidationError('Resolution evidence is not registered and intact')
                current_evidence = {r['path'] for r in revision['evidence'] if r['kind'] in {'measurements', 'verification', 'diagnostics', 'final'}}
                if not any(link.split('#', 1)[0] in current_evidence for link in decision['evidence']):
                    raise ValidationError('Resolution needs independent evidence from this revision: ' + task['id'])
                before_id = task['responses'][-1].get('after_revision')
                if before_id and before_id != revision['id'] and not task.get('comparisons', {}).get(revision['id']):
                    raise ValidationError('Correction lacks matching before/after renders')
            decisions.append((task, decision))
        for finding in result['findings']:
            if not isinstance(finding, dict) or not finding.get('instruction') or not finding.get('requirement'):
                raise ValidationError('Every actionable finding needs instruction and requirement')
        for task, decision in decisions:
            task['status'] = 'verifier_resolved' if decision['resolved'] else 'open'
            task['history'].append({'at': utc_now(), 'type': task['status'], 'revision': revision['id'], **decision})
            if decision['resolved'] and task['source'] == 'human':
                from .util import append_jsonl
                append_jsonl(run_dir / 'feedback/events.jsonl', {'type': 'feedback_resolved', 'feedback_ids': [task['id']], 'revision': revision['id'], 'at': utc_now()})
        # Commit new findings and the verifier outcome in one ledger transaction.
        for finding in result['findings']:
            stable_id = 'task_' + canonical_hash(['agent_judgment', finding['requirement'], finding['instruction']])[:16]
            task = next((t for t in value['tasks'] if t['id'] == stable_id), None)
            if task:
                task['status'] = 'open'
                task['history'].append({'at': utc_now(), 'type': 'reopened', 'revision': revision['id']})
            else:
                value['tasks'].append({'id': stable_id, 'requirement': finding['requirement'], 'instruction': finding['instruction'],
                    'source': 'agent_judgment', 'severity': finding.get('severity','error'), 'status': 'open',
                    'evidence': finding.get('evidence', []), 'responses': [], 'history':[{'at': utc_now(), 'type':'opened'}]})
        result_path = run_dir / 'revisions' / revision['id'] / 'verification' / 'verdict.json'
        atomic_write_json(result_path, result)
        revision['verification'] = result
        revision['verification_receipt'] = {'path': result_path.relative_to(run_dir).as_posix(), 'sha256': sha256_file(result_path)}
        records = [{'path': (result_path.parent / r['path']).relative_to(run_dir).as_posix(), 'sha256': r['sha256'], 'size': r['size'], 'kind': 'builder_claim' if 'claim_snapshots' in (result_path.parent / r['path']).parts else 'verification'} for r in file_inventory(result_path.parent)]
        revision['evidence'] = [r for r in revision['evidence'] if r['kind'] != 'verification' and not r['path'].startswith(result_path.parent.relative_to(run_dir).as_posix() + '/')] + records
        exploration = value.get('exploration', {})
        if exploration.get('enabled'):
            from .design import AuditDecision, DecisionAction, EvaluationAxes, ExplorationState, transition
            state_fields = ExplorationState.__dataclass_fields__
            state = ExplorationState(**{key: exploration[key] for key in state_fields if key in exploration})
            audits = exploration.get('audits', [])
            concept_id = exploration.get('current_concept_id', state.current_concept_id)
            previous = next((item for item in reversed(audits) if item.get('concept_id') == concept_id), None)
            total = sum(float(raw_scores[key]) for key in ('accuracy', 'precision', 'assembly', 'physical_viability', 'aesthetic', 'evidence'))
            improved = previous is None or total > float(previous.get('score_total', -1)) + 1
            blockers = tuple(sorted({str(item.get('requirement')) for item in result['findings'] if item.get('severity', 'error') == 'error'}))
            audit = AuditDecision(DecisionAction(decision), result['assessment'], EvaluationAxes.from_scores(raw_scores), blockers, improved)
            next_state = transition(state, audit)
            audit_record = {'revision': revision['id'], 'concept_id': concept_id, 'cycle': state.cycles, 'phase': state.phase,
                            'refinement_round': state.refinements, 'decision': decision, 'scores': dict(raw_scores),
                            'score_total': total, 'blocking_requirements': list(blockers), 'improved': improved}
            audits.append(audit_record)
            record = next_state.record()
            if next_state.phase == 'refine':
                candidates = [item for item in audits if item.get('cycle') == next_state.cycles]
                selected = max(candidates, key=lambda item: (not item.get('blocking_requirements'), item.get('score_total', 0)))
                record['selected_concept_id'] = selected['concept_id']
                record['current_concept_id'] = selected['concept_id']
            value['exploration'] = {**record, 'audits': audits}
            revision['design_audit'] = audit_record
            event(value, 'design_decision', **audit_record)
        retain_candidates(run_dir, value)
        event(value, 'verified', revision=revision['id'], verdict=result['verdict'], decision=decision)
        save(run_dir, value)


def assert_publishable(run_dir, artifact_sha256):
    value = load(run_dir)
    revision = current(run_dir)
    integrity(run_dir, revision)
    for key in ('evaluation', 'verification'):
        receipt = revision.get(key + '_receipt')
        if not receipt:
            raise StateError('Missing independent result receipt: ' + key)
        path = resolve_within(run_dir, receipt['path'])
        if sha256_file(path) != receipt['sha256'] or read_json(path) != revision.get(key):
            raise StateError('Ledger diverges from independent result evidence: ' + key)
    control = read_json(run_dir / 'control.json', {})
    if control.get('cancel_requested') or control.get('pause_requested'):
        raise StateError('Publication paused or cancelled')
    evaluation, verification = revision.get('evaluation') or {}, revision.get('verification') or {}
    from .discovery import verify_requirements
    verify_requirements(run_dir, revision, verification, publication=True)
    if revision['sha256'] != artifact_sha256 or evaluation.get('source_sha256') != artifact_sha256 or evaluation.get('passed') is not True:
        raise StateError('Deterministic measurements did not pass for this artifact')
    if not evaluation.get('checks') or any(c.get('status') == 'fail' for c in evaluation['checks']):
        raise StateError('Missing or failed deterministic checks')
    if verification.get('verdict') != 'pass' or verification.get('artifact_sha256') != artifact_sha256 or verification.get('acceptance_sha256') != value['acceptance_sha256']:
        raise StateError('Independent verifier has not passed this exact artifact')
    if any(t['status'] != 'verifier_resolved' for t in value['tasks']):
        raise StateError('Unresolved correction tasks prevent publication')
    if not {'measurements', 'diagnostics', 'verification', 'final'}.issubset({r['kind'] for r in revision['evidence']}):
        raise StateError('Required review evidence is missing')
    from .automation import validate_render_set
    for kind in ('diagnostics', 'final'):
        directory = run_dir / 'revisions' / revision['id'] / kind
        rendered = read_json(directory / 'blender_result.json')
        hashes = validate_render_set(run_dir, directory, rendered, kind, artifact_sha256)
        registered = {r['path']: r['sha256'] for r in revision['evidence'] if r['kind'] == kind and r['path'].endswith('.png')}
        expected = {(directory / relative).relative_to(run_dir).as_posix(): digest for relative, digest in hashes.items()}
        if registered != expected:
            raise StateError('Required render evidence is not registered intact: ' + kind)
    for record in revision['evidence']:
        if sha256_file(resolve_within(run_dir, record['path'])) != record['sha256']:
            raise StateError('Review evidence changed: ' + record['path'])
    return revision


def package(run_dir, destination):
    revision = current(run_dir)
    assert_publishable(run_dir, revision['sha256'])
    destination.mkdir(parents=True, exist_ok=True)
    for name in ('acceptance.json', 'review.json', 'run.json', 'model-settings.initial.json', 'model-settings.json', 'automation.json', 'parent.json', 'inherited-support.json'):
        source = run_dir / name
        if source.is_file():
            shutil.copy2(source, destination / name)
    for name in ('revisions', 'artifacts', 'snapshot', 'feedback', 'evidence', 'checkpoints', 'logs', 'discovery'):
        source = run_dir / name
        if source.is_dir():
            shutil.copytree(source, destination / name, dirs_exist_ok=True)
    for rev in load(run_dir)['revisions']:
        for record in rev['evidence']:
            source = resolve_within(run_dir, record['path'])
            target = destination / record['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    # Human-facing package links are relative; original invocation logs retain observed paths as provenance.
    import html
    ledger = load(run_dir)
    sections = []
    for rev in ledger['revisions']:
        images = [r for r in rev['evidence'] if r['path'].endswith('.png') and r['kind'] in {'diagnostics', 'final'}]
        links = ''.join('<a href="' + html.escape(r['path'], quote=True) + '"><img loading="lazy" src="' + html.escape(r['path'], quote=True) + '" alt="' + html.escape(r['path'], quote=True) + '"></a>' for r in images)
        sections.append('<section><h2>' + html.escape(rev['id']) + '</h2><p><a href="' + html.escape(rev['artifact'], quote=True) + '">Owned Blender artifact</a> ? ' + html.escape(rev['sha256']) + '</p><div class="images">' + links + '</div></section>')
    text = '<!doctype html><html lang="en"><meta charset="utf-8"><title>ModelingBench review package</title><style>body{font:16px system-ui;margin:3rem auto;max-width:1100px;padding:1rem;color:#252525;background:#f6f5f2}.images{display:grid;grid-template-columns:repeat(4,1fr);gap:1rem}img{width:100%}pre{white-space:pre-wrap;overflow-wrap:anywhere}a:focus{outline:3px solid #397cba}</style><h1>Verified asset review</h1><p><a href="acceptance.json">Acceptance checklist</a> ? <a href="review.json">Findings and correction history</a> ? <a href="model-settings.json">Model settings and provenance</a> ? <a href="manifest.json">File hashes</a></p>' + ''.join(sections) + '<details><summary>Correction timeline</summary><pre>' + html.escape(__import__('json').dumps(ledger['tasks'], indent=2)) + '</pre></details></html>'
    (destination / 'index.html').write_text(text, encoding='utf-8')
    atomic_write_json(destination / 'manifest.json', {'version': 1, 'artifact_sha256': revision['sha256'], 'files': file_inventory(destination, exclude={'manifest.json'})})


def comparisons(run_dir):
    with lock(run_dir):
        value = load(run_dir)
        after = value['revisions'][-1]
        for task in value['tasks']:
            if not task['responses']: continue
            before_id = task['responses'][-1]['after_revision']
            before = next((r for r in value['revisions'] if r['id'] == before_id), None)
            if before is None or before['id'] == after['id']: continue
            def images(revision):
                return {r['path'].split('/diagnostics/', 1)[1]: r for r in revision['evidence']
                        if r['kind'] == 'diagnostics' and '/diagnostics/' in r['path'] and r['path'].endswith('.png')}
            old, new = images(before), images(after)
            matched = [{'view': k, 'before': old[k], 'after': new[k]} for k in sorted(old.keys() & new.keys())]
            task.setdefault('comparisons', {})[after['id']] = matched
        save(run_dir, value)
