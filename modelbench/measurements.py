"""Independent, cacheable Blender measurement evaluation."""
from __future__ import annotations
import subprocess
from pathlib import Path
from typing import Any
from .blender import _blender_environment, find_blender
from .errors import BlenderError, StateError
from .budgets import BudgetExhausted, bounded_timeout, deadline_expired
from .runs import verify_run_snapshot
from .util import atomic_write_json, canonical_hash, read_json, sha256_file, utc_now

def _driver(run_dir: Path) -> Path:
    path=run_dir/'snapshot'/'measure_driver.py'
    if not path.is_file() or path.is_symlink(): raise StateError('Run-owned measurement evaluator snapshot is missing')
    return path
def _source_hash(run_dir: Path, artifact: Path, owned: bool) -> str:
    if artifact.is_symlink() or not artifact.is_file(): raise StateError('Measurement source must be a regular artifact')
    actual=sha256_file(artifact)
    if owned:
        state=read_json(run_dir/'state.json',{}); expected=(state.get('artifact') or {}).get('sha256') if isinstance(state,dict) else None
        if not isinstance(expected,str) or actual!=expected: raise StateError('Measurement artifact does not match the owned artifact')
    return actual
def _valid_cache(value: Any, source: str, evaluator: str) -> bool:
    if not isinstance(value,dict) or value.get('source_sha256')!=source or value.get('evaluator_sha256')!=evaluator: return False
    recorded=value.get('cache_payload_sha256'); payload={k:v for k,v in value.items() if k!='cache_payload_sha256'}
    return isinstance(recorded,str) and recorded==canonical_hash(payload)
def evaluate(root: Path, run_dir: Path, artifact: Path, output_dir: Path, *, owned: bool=True, budget_deadline: float | None=None) -> dict[str, Any]:
    """Measure an artifact, binding cache, input bytes, task and snapshot evaluator exactly."""
    if owned: verify_run_snapshot(run_dir)
    source=_source_hash(run_dir,artifact,owned); task=read_json(run_dir/'snapshot'/'task.json')
    if not isinstance(task,dict): raise StateError('Measurement task snapshot is invalid')
    driver=_driver(run_dir); evaluator=sha256_file(driver); key=canonical_hash({'artifact':source,'task':task,'evaluator':evaluator}); output_dir.mkdir(parents=True,exist_ok=True); file_key=key[:16]; cache=output_dir/f'measurement_{file_key}.json'
    cached=read_json(cache)
    if _valid_cache(cached,source,evaluator) and cached.get('cache_key') == key and _source_hash(run_dir,artifact,owned)==source: return cached
    result_path=output_dir/f'measurement_{file_key}.result.json'; invocation=output_dir/f'measurement_{file_key}.invocation.json'; metadata=read_json(run_dir/'run.json',{}); profile=metadata.get('render_profiles',{}).get('final',{}).get('data',{}) if isinstance(metadata,dict) else {}
    if not isinstance(profile,dict): raise StateError('Run final render profile is invalid')
    atomic_write_json(invocation,{'schema_version':1,'source_blend':str(artifact.resolve()),'source_sha256':source,'result_path':str(result_path.resolve()),'task':task,'evaluator_sha256':evaluator,'started_at':utc_now()})
    try: process=subprocess.run([find_blender(profile),'--background','--factory-startup','--disable-autoexec','--python',str(driver),'--',str(invocation)],cwd=root,env=_blender_environment(),text=True,capture_output=True,timeout=bounded_timeout(int(profile.get('measurement_seconds',600)),budget_deadline),check=False)
    except subprocess.TimeoutExpired as exc:
        if deadline_expired(budget_deadline): raise BudgetExhausted('User budget exhausted') from exc
        raise BlenderError('Blender measurement timed out') from exc
    log=run_dir/'logs'/f'measure_{key}'; log.with_suffix('.stdout.log').write_text(process.stdout,encoding='utf-8'); log.with_suffix('.stderr.log').write_text(process.stderr,encoding='utf-8')
    if process.returncode: raise BlenderError(f'Blender measurement failed with exit code {process.returncode}')
    if _source_hash(run_dir,artifact,owned)!=source: raise StateError('Measurement source hash changed during evaluation')
    result=read_json(result_path)
    if not isinstance(result,dict) or result.get('ok') is not True or result.get('source_sha256')!=source: raise BlenderError('Blender measurement did not bind a successful result to the source')
    result['evaluator_sha256']=evaluator; result['cache_key']=key; result['cache_payload_sha256']=canonical_hash(result); atomic_write_json(cache,result); return result
