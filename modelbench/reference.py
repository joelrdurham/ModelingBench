"""Harness bridge to the independently versioned reconstruction engine and Blender renderer."""
from __future__ import annotations
import copy
import json
import math
import subprocess
from pathlib import Path
from .errors import StateError, ValidationError, BlenderError
from .util import atomic_write_json, canonical_hash, read_json, sha256_file


def solve_references(run_dir, spec, directory):
    from .reconstruction_bridge import solve_references as execute_references
    return execute_references(run_dir, spec, directory)


def _validate_files(run_dir, records):
    from .reconstruction_bridge import validate_records
    validate_records(run_dir, records)


def render_reference(root, run_dir, artifact, output, reference, solution, task, budget_deadline=None):
    from .blender import find_blender, _blender_environment
    from .budgets import bounded_timeout, deadline_expired, BudgetExhausted
    from .config import load_toml
    driver = run_dir / 'snapshot/reference_driver.py'
    if not driver.is_file(): raise StateError('Run-owned reference bridge missing')
    profile = load_toml(run_dir / 'snapshot/final_profile.toml')
    output.mkdir(parents=True, exist_ok=True)
    payload = {'source_blend': str(artifact), 'source_sha256': sha256_file(artifact), 'result_path': str(output / 'result.json'),
        'output_dir': str(output), 'task': task, 'render_profile': profile,
        'reference': {'width': solution['size'][0], 'height': solution['size'][1], 'camera': solution['solution']['camera'],
            'bindings': reference.get('bindings', []), 'depth_exr': True}}
    from .reconstruction_bridge import BRIDGE_VERSION
    payload['reference_identity'] = {'bridge_version': BRIDGE_VERSION, 'artifact_sha256': payload['source_sha256'],
        'reconstruction_receipt_sha256': solution.get('execution_receipt_sha256'),
        'feature_binding_sha256': canonical_hash(reference.get('bindings', [])),
        'selected_hypothesis': solution.get('selected_hypothesis'), 'camera_fixed': True}
    atomic_write_json(output / 'invocation.json', payload)
    try:
        result = subprocess.run([find_blender(profile), '--background', '--factory-startup', '--disable-autoexec', '--python', str(driver), '--', str(output / 'invocation.json')],
            cwd=root, env=_blender_environment(), text=True, capture_output=True,
            timeout=bounded_timeout(profile.get('measurement_seconds', 600), budget_deadline), check=False)
    except subprocess.TimeoutExpired as exc:
        if deadline_expired(budget_deadline): raise BudgetExhausted('User budget exhausted') from exc
        raise BlenderError('Reference render timed out') from exc
    (output / 'stdout.log').write_text(result.stdout, encoding='utf-8'); (output / 'stderr.log').write_text(result.stderr, encoding='utf-8')
    value = read_json(output / 'result.json', {})
    if result.returncode or value.get('ok') is not True: raise BlenderError('Reference bridge failed: ' + str(value.get('error', result.returncode)))
    if value.get('source_sha256') != payload['source_sha256'] or sha256_file(artifact) != payload['source_sha256']: raise StateError('Reference render artifact changed')
    value['reference_identity'] = payload['reference_identity']
    return value


def measure_image_fit(reference, rendered, source_image, output):
    """Compare independent geometry projections/mask to audited annotations; never camera-fitting loss."""
    import numpy as np
    from PIL import Image, ImageDraw
    from scipy.ndimage import distance_transform_edt
    rgba = Image.open(rendered['render']['rgba']).convert('RGBA')
    width, height = rgba.size
    overlay = Image.open(source_image).convert('RGB').resize(rgba.size)
    draw = ImageDraw.Draw(overlay)
    points = {p['id']: p for p in rendered.get('bindings', [])}
    squared, normalized, missing, curves, occlusions = [], [], [], {}, []
    projected_by_id = {}
    for binding in reference.get('bindings', []):
        values = points.get(binding['id'], {}).get('points', [])
        if len(values) != 1 or values[0].get('pixel') is None:
            missing.append(binding['id']); continue
        actual = np.array(values[0]['pixel']) / [width, height]
        expected = np.array(binding['image'], dtype=float)
        projected_by_id[binding['id']] = (actual, expected)
        error = float(np.linalg.norm(actual - expected))
        sigma = float(binding.get('uncertainty', .002))
        if not math.isfinite(sigma) or sigma <= 0: raise ValidationError('Annotation uncertainty must be positive')
        squared.append(error ** 2); normalized.append((error / sigma) ** 2)
        ax, ay = actual * [width, height]; ex, ey = expected * [width, height]
        draw.ellipse((ex-3, ey-3, ex+3, ey+3), outline='green', width=2)
        draw.line((ex, ey, ax, ay), fill='red', width=2)
        if binding.get('curve_id'): curves.setdefault(binding['curve_id'], []).append((actual.tolist(), expected.tolist()))
        if 'visible' in binding: occlusions.append(values[0].get('visible') != binding['visible'])
    metrics = {'point_rms_normalized': math.sqrt(sum(squared) / len(squared)) if squared and not missing else None,
        'point_rms_uncertainty': math.sqrt(sum(normalized) / len(normalized)) if normalized and not missing else None,
        'occlusion_mismatches': sum(occlusions) if occlusions and not missing else None}
    if curves and not missing:
        distances = []
        for samples in curves.values():
            a = np.array([p[0] for p in samples]); b = np.array([p[1] for p in samples]); matrix = np.linalg.norm(a[:, None, :] - b[None, :, :], axis=2)
            distances.extend(matrix.min(axis=0)); distances.extend(matrix.min(axis=1))
        metrics['symmetric_curve_distance'] = float(np.mean(distances))
    conic_errors = []
    for conic in reference.get('conics', []):
        coefficients = conic['coefficients']
        a,b,c,d,e,f = coefficients
        for ident in conic['binding_ids']:
            if ident not in projected_by_id: missing.append(ident); continue
            x,y = projected_by_id[ident][0]
            gradient = math.hypot(2*a*x+b*y+d, b*x+2*c*y+e)
            if gradient > 1e-12: conic_errors.append(abs(a*x*x+b*x*y+c*y*y+d*x+e*y+f)/gradient)
    metrics['conic_residual'] = float(np.mean(conic_errors)) if conic_errors and not missing else None
    phase_errors = []
    for pattern in reference.get('repeated_patterns', []):
        center = np.array(pattern['image_center'], dtype=float)
        for ident in pattern['binding_ids']:
            if ident not in projected_by_id: missing.append(ident); continue
            actual, expected = projected_by_id[ident]
            delta = math.atan2(*(actual-center)[::-1])-math.atan2(*(expected-center)[::-1])
            phase_errors.append(abs(math.atan2(math.sin(delta),math.cos(delta))))
    metrics['repetition_phase'] = float(np.mean(phase_errors)) if phase_errors and not missing else None
    silhouette = reference.get('silhouette')
    if silhouette:
        target = Image.new('L', rgba.size); ImageDraw.Draw(target).polygon([(x*width, y*height) for x,y in silhouette], fill=255)
        expected = np.array(target) > 0; actual = np.array(rgba)[:, :, 3] > 127
        union = np.logical_or(actual, expected).sum()
        metrics['silhouette_iou'] = float(np.logical_and(actual, expected).sum() / union) if union else None
        d_actual, d_expected = distance_transform_edt(~actual), distance_transform_edt(~expected)
        metrics['silhouette_distance'] = float((d_actual[expected].mean() + d_expected[actual].mean()) / (2 * max(width,height))) if actual.any() and expected.any() else None
        delta = np.zeros((height,width,3),dtype=np.uint8); delta[expected & ~actual] = [0,220,80]; delta[actual & ~expected] = [240,30,30]
        Image.fromarray(delta).save(output / 'silhouette_difference.png')
        heat = np.minimum(np.maximum(d_actual,d_expected) / max(width,height) * 2550,255).astype('uint8')
        Image.fromarray(heat).save(output / 'distance_heatmap.png')
        target.save(output / 'reference_mask.png'); Image.fromarray(actual.astype('uint8')*255).save(output / 'model_mask.png')
    overlay.save(output / 'landmark_overlay.png')
    for index, region in enumerate(reference.get('regions', [])):
        x,y,w,h = region['crop']; overlay.crop((int(x*width),int(y*height),int((x+w)*width),int((y+h)*height))).save(output / f'region_{index:04d}.png')
    return {'metrics': metrics, 'missing_bindings': missing, 'coverage': {'points': len(squared), 'curves': len(curves), 'silhouette': bool(silhouette), 'occlusion_samples': len(occlusions)}}


def evaluate_references(root, run_dir, artifact, output, evaluation, budget_deadline=None):
    from .discovery import enabled, active_record, active_spec, effective_task
    if not enabled(run_dir): return evaluation
    record = active_record(run_dir); spec = active_spec(run_dir)
    refs = spec.get('references', [])
    required = [r for r in spec['requirements'] if r['method'] == 'reference']
    if not refs: return evaluation
    index = record.get('reconstructions', [])
    _validate_files(run_dir, index)
    by_id = {r['id']: r for r in index}; results = {}; checks = []
    for ref in refs:
        solution = by_id.get(ref['id'])
        requirements = [r for r in required if r['reference_id'] == ref['id']]
        folder = output / ('reference_' + str(refs.index(ref)+1).zfill(4))
        supported = bool(solution and solution['status'] == 'solved' and solution['solution'].get('camera'))
        checks.append({'id': 'reconstruction_support_' + ref['id'], 'requirement': 'Reconstruction supports ' + ref['id'],
            'status': 'pass' if supported else 'unknown', 'assessment': 'assessed' if supported else 'unassessed',
            'category': 'reconstruction_assumption', 'evidence_source': 'reconstruction', 'critical': any(r['critical'] for r in requirements),
            'reconstruction_receipt_sha256': solution.get('execution_receipt_sha256') if solution else None})
        if not solution or solution['status'] != 'solved' or not evaluation.get('passed'):
            fit = {'metrics': {}, 'assessment': 'unassessed', 'category': 'reconstruction_assumption' if not supported else 'model_geometry', 'reason': 'Unresolved reconstruction or failed geometric gate'}
        else:
            rendered = render_reference(root, run_dir, artifact, folder, ref, solution, effective_task(run_dir), budget_deadline)
            fit = measure_image_fit(ref, rendered, run_dir / solution['path'] / 'reference.png', folder)
            fit['assessment'] = 'assessed'
            fit['category'] = 'model_geometry'
            fit['reference_identity'] = rendered.get('reference_identity')
            if solution['solution'].get('rectification') and ref['case'].get('case_version') == '1.0':
                rectified_compare(ref['case'], solution['solution']['rectification'], run_dir / solution['path'] / 'reference.png', Path(rendered['render']['rgba']), folder)
        for req in requirements:
            metric = req.get('check', {}).get('metric', 'point_rms_uncertainty'); actual = fit['metrics'].get(metric)
            threshold = req.get('check', {}).get('threshold')
            if isinstance(threshold, bool) or not isinstance(threshold, (int,float)) or not math.isfinite(threshold): raise ValidationError('Reference requirement needs finite threshold')
            passed = actual is not None and (actual >= threshold if metric == 'silhouette_iou' else actual <= threshold)
            checks.append({'id': req['id'], 'requirement': req['criterion'], 'status': 'pass' if passed else 'unknown' if actual is None else 'fail',
                'expected': threshold, 'actual': actual, 'method': metric, 'critical': req['critical'], 'evidence_source': 'measured',
                'category': 'model_geometry' if supported else 'reconstruction_assumption', 'check_kind': 'model_to_reference',
                'assessment': 'assessed' if actual is not None else 'unassessed',
                'specification_sha256': record['specification']['sha256']})
        results[ref['id']] = fit
    value = copy.deepcopy(evaluation); value['checks'].extend(checks)
    value['passed'] = value['passed'] and all(c['status'] == 'pass' for c in checks if c['critical'])
    value['reference_evaluator_sha256'] = sha256_file(Path(__file__))
    from .reconstruction_bridge import evaluation_identity
    value['reference_identity'] = evaluation_identity(run_dir, sha256_file(artifact), record, spec)
    value['reference_cache_key'] = canonical_hash(value['reference_identity'])
    value['reference_fit'] = results; value['specification_sha256'] = record['specification']['sha256']
    atomic_write_json(output / 'evaluation.json', value)
    return value


def rectified_compare(case, rectification, source_image, model_image, output):
    import numpy as np
    from PIL import Image
    from scipy.ndimage import map_coordinates
    plane = next((p for p in case.get('planes', []) if p.get('id') == rectification.get('plane_id')), None)
    if not plane: return
    points = np.array(plane['rectification']['plane_points'], dtype=float)
    low, high = points.min(axis=0), points.max(axis=0)
    if np.any(high <= low): return
    ys, xs = np.mgrid[0:256,0:256]
    coordinates = np.stack((low[0] + xs/255*(high[0]-low[0]), low[1] + ys/255*(high[1]-low[1]), np.ones_like(xs)), axis=0).reshape(3,-1)
    projected = np.array(rectification['homography_plane_to_image']) @ coordinates
    denominator = projected[2]; denominator[np.abs(denominator) < 1e-12] = np.nan
    uv = projected[:2] / denominator
    images = []
    for path in (source_image, model_image):
        image = np.array(Image.open(path).convert('RGB')); h,w = image.shape[:2]
        coords = np.stack((uv[1]*(h-1),uv[0]*(w-1)))
        pixels = np.stack([map_coordinates(image[:,:,c],coords,order=1,mode='constant') for c in range(3)],axis=-1).reshape(256,256,3).astype('uint8')
        images.append(pixels)
    Image.fromarray(images[0]).save(output/'rectified_reference.png')
    Image.fromarray(images[1]).save(output/'rectified_model.png')
    Image.fromarray(((images[0].astype(float)+images[1])/2).astype('uint8')).save(output/'rectified_overlay.png')
