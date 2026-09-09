"""Assembly-aware design manifest validation and exploration state.

This module deliberately has no dependency on the geometry backends.  It is a
small, serialisable contract between a design brief, the measurements that
support it, and the decisions made while exploring alternatives.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from typing import Any, Iterable, Mapping


class DesignManifestError(ValueError):
    """Raised when a manifest is incomplete or internally inconsistent."""


class EvidenceStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


class DecisionAction(str, Enum):
    ACCEPT = 'ACCEPT'
    REFINE = 'REFINE'
    NEW_CONCEPT = 'NEW_CONCEPT'
    BEST_AVAILABLE = 'BEST_AVAILABLE'


def requirement_id(kind: str, *parts: str) -> str:
    """Return a stable requirement identifier; identifiers are not display text."""
    if not kind or not all(str(part).strip() for part in parts):
        raise DesignManifestError("requirement IDs need a kind and non-empty parts")
    return ":".join((kind.strip().lower(), *(str(part).strip().lower() for part in parts)))


@dataclass(frozen=True, slots=True)
class ComponentSpec:
    id: str
    name: str
    role: str = "part"
    critical: bool = False
    quantity: int = 1
    separate_part: bool = True
    material_assumptions: tuple[str, ...] = ()
    manufacturing_assumptions: tuple[str, ...] = ()
    required_geometric_evidence: tuple[str, ...] = ()

    @property
    def requirement_id(self) -> str:
        return requirement_id("component", self.id)

    @property
    def identity(self) -> str:
        return self.name

    @property
    def criticality(self) -> str:
        return 'critical' if self.critical else 'major'


@dataclass(frozen=True, slots=True)
class InterfaceSpec:
    id: str
    name: str
    participants: tuple[str, ...]
    relationship: str
    critical: bool = True
    verification_methods: tuple[str, ...] = ()
    degrees_of_freedom: tuple[str, ...] = ()
    mating_geometry: str = ''
    fits: str = ''
    engagement: str = ''
    clearance: str = ''
    retention: str = ''
    load_path: str = ''
    motion_envelope: str = ''

    @property
    def requirement_id(self) -> str:
        return requirement_id("interface", self.id)

    @property
    def components(self) -> tuple[str, ...]:
        return self.participants

    @property
    def joint_type(self) -> str:
        return self.relationship

    @property
    def criticality(self) -> str:
        return 'critical' if self.critical else 'major'


@dataclass(frozen=True, slots=True)
class RepeatedFeatureSpec:
    component_id: str
    feature: str
    expected_count: int
    pattern: str
    critical: bool = False
    verification_methods: tuple[str, ...] = ()
    id: str = ''
    spacing_or_phase: str = ''
    representative_dimensions: tuple[tuple[str, Any], ...] = ()
    symmetry_rules: str = ''
    malformed_instance_criteria: tuple[str, ...] = ()

    @property
    def requirement_id(self) -> str:
        return requirement_id('feature', self.id) if self.id else requirement_id('feature', self.component_id, self.feature)

    @property
    def feature_type(self) -> str:
        return self.feature

    @property
    def pattern_axis_or_path(self) -> str:
        return self.pattern

    @property
    def criticality(self) -> str:
        return 'critical' if self.critical else 'major'


@dataclass(frozen=True, slots=True)
class GeometryEvidence(Mapping[str, Any]):
    requirement_id: str
    status: EvidenceStatus
    method: str = ""
    provenance: str = ""
    artifacts: tuple[str, ...] = ()
    measured_value: Any = None
    tolerance: Any = None
    inspected_entities: tuple[str, ...] = ()
    severity: str = 'error'
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", EvidenceStatus(self.status))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(self, 'inspected_entities', tuple(self.inspected_entities))
        object.__setattr__(self, 'inspected_entities', tuple(self.inspected_entities))
        if not self.requirement_id:
            raise DesignManifestError("evidence needs a requirement_id")
        if self.status is not EvidenceStatus.UNKNOWN and not (self.provenance or self.artifacts):
            raise DesignManifestError("pass/fail evidence needs provenance or an artifact")
        if self.status is not EvidenceStatus.UNKNOWN and (not self.method or not self.inspected_entities):
            raise DesignManifestError('pass/fail evidence needs a measurement method and inspected entities')
        if self.severity not in {'error', 'warning'}:
            raise DesignManifestError('evidence severity must be error or warning')
        if self.status is not EvidenceStatus.UNKNOWN and (not self.method or not self.inspected_entities):
            raise DesignManifestError('pass/fail evidence needs a measurement method and inspected entities')
        if self.severity not in {'error', 'warning'}:
            raise DesignManifestError('evidence severity must be error or warning')

    def __getitem__(self, key: str) -> Any:
        if key not in self.__dataclass_fields__:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self):
        return iter(self.__dataclass_fields__)

    def __len__(self) -> int:
        return len(self.__dataclass_fields__)


@dataclass(frozen=True, slots=True)
class EvaluationAxes:
    geometry: float = 0.0
    assembly: float = 0.0
    manufacturability: float = 0.0
    verification: float = 0.0
    reference_accuracy: float = 0.0
    dimensional_precision: float = 0.0
    assembly_completeness: float = 0.0
    physical_viability: float = 0.0
    aesthetic_quality: float = 0.0
    evidence_quality: float = 0.0

    def __post_init__(self) -> None:
        values = (self.geometry, self.assembly, self.manufacturability, self.verification,
                  self.reference_accuracy, self.dimensional_precision, self.assembly_completeness,
                  self.physical_viability, self.aesthetic_quality, self.evidence_quality)
        if any(not 0.0 <= value <= 100.0 for value in values):
            raise DesignManifestError('evaluation axes must be in [0, 100]')

    @classmethod
    def from_scores(cls, scores: Mapping[str, Any]) -> 'EvaluationAxes':
        return cls(reference_accuracy=float(scores.get('accuracy', 0)), dimensional_precision=float(scores.get('precision', 0)),
                   assembly_completeness=float(scores.get('assembly', 0)), physical_viability=float(scores.get('physical_viability', 0)),
                   aesthetic_quality=float(scores.get('aesthetic', 0)), evidence_quality=float(scores.get('evidence', 0)))


@dataclass(frozen=True, slots=True)
class AuditDecision:
    action: DecisionAction
    rationale: str
    axes: EvaluationAxes = field(default_factory=EvaluationAxes)
    blocking_requirements: tuple[str, ...] = ()
    improved: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", DecisionAction(self.action))
        object.__setattr__(self, "blocking_requirements", tuple(self.blocking_requirements))
        if not self.rationale.strip():
            raise DesignManifestError("an audit decision needs a rationale")
        if self.action is DecisionAction.ACCEPT and self.blocking_requirements:
            raise DesignManifestError("a blocked design cannot be accepted")


@dataclass(frozen=True, slots=True)
class DesignManifest:
    components: tuple[ComponentSpec, ...]
    interfaces: tuple[InterfaceSpec, ...] = ()
    repeated_features: tuple[RepeatedFeatureSpec, ...] = ()
    evidence: tuple[GeometryEvidence, ...] = ()

    @property
    def requirements(self) -> tuple[str, ...]:
        return tuple(x.requirement_id for x in self.components + self.interfaces + self.repeated_features)

    def record(self) -> dict[str, Any]:
        return {'version': 1, 'components': [asdict(x) for x in self.components],
                'interfaces': [asdict(x) for x in self.interfaces],
                'repeated_features': [asdict(x) for x in self.repeated_features],
                'requirement_ids': list(self.requirements)}


def _items(value: Any, label: str) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise DesignManifestError(f"{label} must be a list")
    if not all(isinstance(item, Mapping) for item in value):
        raise DesignManifestError(f"{label} entries must be mappings")
    return list(value)


def _strings(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if not isinstance(value, (list, tuple)) or not all(isinstance(x, str) and x.strip() for x in value):
        raise DesignManifestError(f"{label} must contain non-empty strings")
    return tuple(value)


def parse_design_manifest(config: Mapping[str, Any]) -> DesignManifest:
    """Parse ``design_manifest`` or derive one from legacy task configuration."""
    if not isinstance(config, Mapping):
        raise DesignManifestError("task config must be a mapping")
    source = config.get("design_manifest")
    if source is None:
        source = _derive_legacy_manifest(config)
    if not isinstance(source, Mapping):
        raise DesignManifestError("design_manifest must be a mapping")
    components = tuple(ComponentSpec(
        id=str(item.get("id", "")).strip(), name=str(item.get("name", item.get("id", ""))).strip(),
        role=str(item.get("role", "part")).strip(), critical=bool(item.get("critical", False)),
        quantity=item.get('quantity', 1), separate_part=bool(item.get('separate_part', True)),
        material_assumptions=_strings(item.get('material_assumptions'), 'material assumptions'),
        manufacturing_assumptions=_strings(item.get('manufacturing_assumptions'), 'manufacturing assumptions'),
        required_geometric_evidence=_strings(item.get('required_geometric_evidence'), 'required geometric evidence'),
    ) for item in _items(source.get("components"), "components"))
    interfaces = tuple(InterfaceSpec(
        id=str(item.get("id", "")).strip(), name=str(item.get("name", item.get("id", ""))).strip(),
        participants=_strings(item.get("participants", item.get("components")), "interface participants"),
        relationship=str(item.get("relationship", item.get("type", ""))).strip(),
        critical=bool(item.get("critical", True)), verification_methods=_strings(item.get("verification_methods"), "verification_methods"),
        degrees_of_freedom=_strings(item.get('degrees_of_freedom'), 'degrees of freedom'),
        mating_geometry=str(item.get('mating_geometry', item.get('role', ''))), fits=str(item.get('fits', '')),
        engagement=str(item.get('engagement', '')), clearance=str(item.get('clearance', '')), retention=str(item.get('retention', '')),
        load_path=str(item.get('load_path', '')), motion_envelope=str(item.get('motion_envelope', '')),
    ) for item in _items(source.get("interfaces"), "interfaces"))
    features = tuple(RepeatedFeatureSpec(
        id=str(item.get("id", "")).strip(), component_id=str(item.get("component_id", item.get("component", ""))).strip(),
        feature=str(item.get("feature", item.get("name", ""))).strip(), expected_count=item.get("expected_count", item.get("count", 0)),
        pattern=str(item.get("pattern", "")).strip(), critical=bool(item.get("critical", False)),
        verification_methods=_strings(item.get("verification_methods"), "verification_methods"),
        spacing_or_phase=str(item.get('spacing_or_phase', item.get('spacing_tolerance', ''))),
        representative_dimensions=tuple(sorted((item.get('representative_dimensions') or {}).items())),
        symmetry_rules=str(item.get('symmetry_rules', '')), malformed_instance_criteria=_strings(item.get('malformed_instance_criteria'), 'malformed instance criteria'),
    ) for item in _items(source.get("repeated_features", source.get("features")), "repeated_features"))
    evidence = tuple(GeometryEvidence(
        requirement_id=str(item.get("requirement_id", "")).strip(), status=item.get("status", EvidenceStatus.UNKNOWN),
        method=str(item.get("method", "")).strip(), provenance=str(item.get("provenance", "")).strip(),
        artifacts=_strings(item.get("artifacts"), "artifacts"), measured_value=item.get('measured_value'), tolerance=item.get('tolerance'),
        inspected_entities=_strings(item.get('inspected_entities'), 'inspected entities'), severity=str(item.get('severity', 'error')),
        note=str(item.get("note", "")).strip(),
    ) for item in _items(source.get("evidence"), "evidence"))
    manifest = DesignManifest(components, interfaces, features, evidence)
    validate_manifest(manifest)
    if evidence:
        validate_evidence(manifest, evidence, require_coverage=False)
    return manifest


def _derive_legacy_manifest(config: Mapping[str, Any]) -> Mapping[str, Any]:
    """Conservative adapter for pre-manifest task configs."""
    assembly = config.get("assembly", {})
    if not isinstance(assembly, Mapping):
        assembly = {}
    raw_components = list(assembly.get("components", config.get("components", [])))
    if not raw_components:
        raw_components = [{"id": "model", "name": "evaluated MODEL collection", "role": "assembly"}]
    raw_interfaces = []
    verification = config.get('verification', {})
    default_methods = verification.get('methods', ['evaluated_geometry', 'interface_section']) if isinstance(verification, Mapping) else ['evaluated_geometry', 'interface_section']
    for item in assembly.get("interfaces", config.get("interfaces", [])):
        copied = dict(item)
        copied.setdefault("relationship", "pinned" if str(item.get("id", "")).startswith("pivot") else "declared_joint")
        copied.setdefault("verification_methods", default_methods)
        raw_interfaces.append(copied)
    feature_table = config.get("features", config.get("repeated_features", []))
    raw_features = feature_table.get("repeated", []) if isinstance(feature_table, Mapping) else feature_table
    features = []
    for item in raw_features:
        copied = dict(item)
        copied.setdefault("component_id", "model")
        copied.setdefault("feature", copied.get("kind", "repeated_feature"))
        copied.setdefault("pattern", "axis_" + str(copied.get("axis", "unknown")))
        copied.setdefault("verification_methods", ["evaluated_instance_inventory", "pattern_measurement"])
        features.append(copied)
    return {"components": raw_components, "interfaces": raw_interfaces, "repeated_features": features}


def validate_manifest(manifest: DesignManifest) -> None:
    if not manifest.components:
        raise DesignManifestError("a design manifest needs at least one component")
    ids = [part.id for part in manifest.components]
    if any(not x for x in ids) or len(ids) != len(set(ids)):
        raise DesignManifestError("component IDs must be non-empty and unique")
    if any(isinstance(part.quantity, bool) or not isinstance(part.quantity, int) or part.quantity < 1 for part in manifest.components):
        raise DesignManifestError('component quantities must be positive integers')
    known = set(ids)
    interface_ids: set[str] = set()
    signatures: dict[tuple[str, ...], str] = {}
    connected: set[str] = set()
    for interface in manifest.interfaces:
        if not interface.id or interface.id in interface_ids:
            raise DesignManifestError("interface IDs must be non-empty and unique")
        interface_ids.add(interface.id)
        if len(interface.participants) < 2 or len(set(interface.participants)) != len(interface.participants):
            raise DesignManifestError(f"interface {interface.id} needs two distinct participants")
        if not set(interface.participants) <= known:
            raise DesignManifestError(f"interface {interface.id} references an unknown component")
        if not interface.relationship:
            raise DesignManifestError(f"interface {interface.id} needs a relationship")
        if interface.critical and not interface.verification_methods:
            raise DesignManifestError(f"critical interface {interface.id} needs verification methods")
        signature = tuple(sorted(interface.participants))
        prior = signatures.get(signature)
        if prior is not None and prior != interface.relationship:
            raise DesignManifestError(f"contradictory interfaces for {', '.join(signature)}")
        signatures[signature] = interface.relationship
        connected.update(interface.participants)
    # A critical component is mechanical scope, therefore it cannot be stranded.
    stranded = [component.id for component in manifest.components if component.critical and component.id not in connected]
    if stranded and len(manifest.components) > 1:
        raise DesignManifestError("critical components must participate in an interface: " + ", ".join(stranded))
    feature_ids: set[str] = set()
    for feature in manifest.repeated_features:
        if feature.component_id not in known or not feature.feature or not isinstance(feature.expected_count, int) or feature.expected_count < 1 or not feature.pattern:
            raise DesignManifestError("repeated features need a known component, name, positive count, and pattern")
        if feature.requirement_id in feature_ids:
            raise DesignManifestError("repeated feature IDs must be unique")
        feature_ids.add(feature.requirement_id)
        if feature.critical and not feature.verification_methods:
            raise DesignManifestError(f"critical feature {feature.requirement_id} needs verification methods")


def validate_evidence(manifest: DesignManifest, evidence: Iterable[GeometryEvidence] | None = None, *, require_coverage: bool = True) -> tuple[str, ...]:
    """Validate evidence and return blocking requirement IDs (fail or unknown)."""
    records = tuple(manifest.evidence if evidence is None else evidence)
    known = set(manifest.requirements)
    by_requirement: dict[str, GeometryEvidence] = {}
    for record in records:
        if record.requirement_id not in known:
            raise DesignManifestError(f"evidence references unknown requirement {record.requirement_id}")
        if record.requirement_id in by_requirement:
            raise DesignManifestError(f"multiple evidence records for {record.requirement_id}")
        by_requirement[record.requirement_id] = record
    critical = {item.requirement_id for item in manifest.interfaces if item.critical}
    critical.update(item.requirement_id for item in manifest.repeated_features if item.critical)
    if require_coverage:
        missing = critical - set(by_requirement)
        if missing:
            raise DesignManifestError("missing evidence for critical requirements: " + ", ".join(sorted(missing)))
    return tuple(sorted(key for key in critical if key not in by_requirement or by_requirement[key].status is not EvidenceStatus.PASS))


def validate_audit_decision(manifest: DesignManifest, decision: AuditDecision, evidence: Iterable[GeometryEvidence] | None = None) -> None:
    """Apply non-compensating gates: a high aggregate score never masks a blocker."""
    blockers = validate_evidence(manifest, evidence, require_coverage=True)
    declared = set(decision.blocking_requirements)
    if declared and declared != set(blockers):
        raise DesignManifestError("decision blockers do not match the evidence")
    if decision.action is DecisionAction.ACCEPT and blockers:
        raise DesignManifestError("critical failed or unavailable measurements forbid acceptance")


@dataclass(frozen=True, slots=True)
class ExplorationState:
    enabled: bool = True
    concepts: int = 1
    refinements: int = 0
    cycles: int = 1
    plateaus: int = 0
    concept_limit: int = 3
    refinement_limit: int = 4
    cycle_limit: int = 2
    finished: bool = False
    phase: str = 'diverge'
    concepts_completed: int = 0
    selected_concept_id: str | None = None
    outcome: str | None = None

    def __post_init__(self) -> None:
        if min(self.concepts, self.refinements, self.cycles, self.plateaus, self.concepts_completed) < 0 or min(self.concept_limit, self.refinement_limit, self.cycle_limit) < 1:
            raise DesignManifestError("exploration counts must be non-negative and limits positive")
        if self.phase not in {'diverge', 'refine', 'finished'}:
            raise DesignManifestError('invalid exploration phase')

    @property
    def current_concept_id(self) -> str:
        return f'cycle_{self.cycles:02d}_concept_{self.concepts:02d}'

    def record(self) -> dict[str, Any]:
        return {**asdict(self), 'current_concept_id': self.current_concept_id}


def advance_exploration(state: ExplorationState, decision: AuditDecision) -> ExplorationState:
    '''Run three-way divergence before refinement and enforce both hard caps.'''
    if state.finished or not state.enabled:
        return state
    if state.cycles >= state.cycle_limit and state.refinements >= state.refinement_limit:
        return replace(state, phase='finished', finished=True, outcome='BEST_AVAILABLE')
    if decision.action is DecisionAction.ACCEPT:
        return replace(state, phase='finished', finished=True, outcome='ACCEPT', selected_concept_id=state.current_concept_id)
    if decision.action is DecisionAction.BEST_AVAILABLE:
        if not (state.cycles >= state.cycle_limit and state.refinements >= state.refinement_limit):
            raise DesignManifestError('BEST_AVAILABLE is valid only at the hard cap')
        return replace(state, phase='finished', finished=True, outcome='BEST_AVAILABLE')
    if state.phase == 'diverge':
        completed = state.concepts_completed + 1
        if completed < state.concept_limit:
            return replace(state, concepts=state.concepts + 1, concepts_completed=completed)
        return replace(state, phase='refine', concepts_completed=completed, refinements=1,
                       selected_concept_id=state.current_concept_id)
    plateau = state.plateaus + (0 if decision.improved else 1)
    redesign = decision.action is DecisionAction.NEW_CONCEPT or plateau > 0 or state.refinements >= state.refinement_limit
    if redesign:
        if state.cycles >= state.cycle_limit:
            return replace(state, phase='finished', finished=True, outcome='BEST_AVAILABLE', plateaus=plateau)
        return replace(state, phase='diverge', cycles=state.cycles + 1, concepts=1, concepts_completed=0,
                       refinements=0, plateaus=0, selected_concept_id=None)
    return replace(state, refinements=state.refinements + 1, plateaus=plateau)


transition = advance_exploration
