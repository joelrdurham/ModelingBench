import unittest

from modelbench.design import (
    AuditDecision, DecisionAction, DesignManifestError, EvidenceStatus,
    EvaluationAxes, ExplorationState, GeometryEvidence, parse_design_manifest,
    transition, validate_audit_decision, validate_evidence,
)


def design_with(evidence=()):
    return parse_design_manifest({"design_manifest": {
        "components": [{"id": "base", "critical": True}, {"id": "lid", "critical": True}],
        "interfaces": [{"id": "hinge", "participants": ["base", "lid"], "relationship": "pinned", "verification_methods": ["measure"]}],
        "repeated_features": [{"id": "mount", "component": "base", "feature": "hole", "count": 4, "pattern": "rectangular", "critical": True, "verification_methods": ["measure"]}],
        "evidence": list(evidence),
    }})


class DesignManifestTests(unittest.TestCase):
    def test_typed_manifest_preserves_assembly_contract_fields(self):
        manifest = parse_design_manifest({'design_manifest': {
            'components': [{'id': 'arm', 'quantity': 2, 'separate_part': True, 'material_assumptions': ['steel'],
                            'manufacturing_assumptions': ['laser cut'], 'required_geometric_evidence': ['plate section']}],
            'repeated_features': [{'id': 'holes', 'component': 'arm', 'feature': 'pin hole', 'count': 4,
                'pattern': 'longitudinal', 'spacing_or_phase': '30 mm pitch', 'representative_dimensions': {'diameter': 12},
                'symmetry_rules': 'paired plates', 'malformed_instance_criteria': ['off-axis', 'duplicate'],
                'verification_methods': ['axis fit']}],
        }})
        self.assertEqual((manifest.components[0].identity, manifest.components[0].quantity), ('arm', 2))
        self.assertEqual(manifest.repeated_features[0].representative_dimensions, (('diameter', 12),))
        self.assertIn('feature:holes', manifest.requirements)

    def test_rejects_incomplete_and_missing_critical_verification(self):
        with self.assertRaises(DesignManifestError):
            parse_design_manifest({"design_manifest": {"components": []}})
        with self.assertRaisesRegex(DesignManifestError, "verification"):
            parse_design_manifest({"design_manifest": {"components": [{"id": "a"}, {"id": "b"}], "interfaces": [{"id": "join", "participants": ["a", "b"], "relationship": "mate"}]}})

    def test_rejects_contradiction_disconnected_hardware_and_wrong_pattern(self):
        with self.assertRaisesRegex(DesignManifestError, "contradictory"):
            parse_design_manifest({"design_manifest": {"components": [{"id": "a"}, {"id": "b"}], "interfaces": [{"id": "i1", "participants": ["a", "b"], "relationship": "bond", "verification_methods": ["inspect"]}, {"id": "i2", "participants": ["b", "a"], "relationship": "slide", "verification_methods": ["inspect"]}]}})
        with self.assertRaisesRegex(DesignManifestError, "participate"):
            parse_design_manifest({"design_manifest": {"components": [{"id": "a", "critical": True}, {"id": "b", "critical": True}]}})
        with self.assertRaisesRegex(DesignManifestError, "pattern"):
            parse_design_manifest({"design_manifest": {"components": [{"id": "a"}], "features": [{"component": "a", "feature": "hole", "count": 4, "pattern": ""}]}})

    def test_unknown_measurement_and_missing_evidence_are_non_compensating(self):
        records = (GeometryEvidence("interface:hinge", EvidenceStatus.PASS, method="coaxiality", provenance="scan", artifacts=("hinge.json",), inspected_entities=("base", "lid")), GeometryEvidence("feature:mount", EvidenceStatus.UNKNOWN, provenance="fixture unavailable"))
        design = design_with(records)
        self.assertEqual(validate_evidence(design), ("feature:mount",))
        with self.assertRaisesRegex(DesignManifestError, "forbid"):
            validate_audit_decision(design, AuditDecision(DecisionAction.ACCEPT, "high scores", EvaluationAxes(1, 1, 1, 1)), records)
        with self.assertRaisesRegex(DesignManifestError, "missing evidence"):
            validate_evidence(design_with())
        with self.assertRaisesRegex(DesignManifestError, "provenance"):
            GeometryEvidence("interface:hinge", EvidenceStatus.FAIL)

    def test_correct_count_does_not_mask_wrong_pattern(self):
        manifest = design_with()
        evidence = [
            GeometryEvidence('interface:hinge', EvidenceStatus.PASS, method='section', inspected_entities=('base', 'lid'), provenance='evaluator'),
            GeometryEvidence('feature:mount', EvidenceStatus.FAIL, method='phase analysis', measured_value={'count': 4, 'spacing': 'wrong'},
                             tolerance='uniform phase', inspected_entities=('four holes',), provenance='evaluator'),
        ]
        self.assertEqual(validate_evidence(manifest, evidence), ('feature:mount',))

    def test_legacy_adapter(self):
        design = parse_design_manifest({"components": [{"id": "a"}, {"id": "b"}], "assembly": {"interfaces": [{"id": "i", "participants": ["a", "b"], "relationship": "mate"}]}, "verification": {"methods": ["inspect"]}})
        self.assertEqual(design.interfaces[0].verification_methods, ("inspect",))


class ExplorationTests(unittest.TestCase):
    def test_three_distinct_concepts_precede_refinement(self):
        state = ExplorationState()
        decision = AuditDecision(DecisionAction.REFINE, 'local defects')
        state = transition(state, decision)
        self.assertEqual((state.phase, state.concepts, state.concepts_completed), ('diverge', 2, 1))
        state = transition(state, decision)
        self.assertEqual((state.phase, state.concepts), ('diverge', 3))
        state = transition(state, decision)
        self.assertEqual((state.phase, state.refinements), ('refine', 1))

    def test_best_available_requires_the_hard_cap(self):
        with self.assertRaisesRegex(DesignManifestError, 'hard cap'):
            transition(ExplorationState(), AuditDecision(DecisionAction.BEST_AVAILABLE, 'premature'))

    def test_plateau_redesign_and_caps(self):
        old = ExplorationState()
        plateau = transition(old, AuditDecision(DecisionAction.REFINE, "flat", improved=False))
        self.assertEqual((old.concepts, plateau.concepts, plateau.cycles), (1, 2, 1))
        redesign = transition(ExplorationState(), AuditDecision(DecisionAction.NEW_CONCEPT, "redesign interface"))
        self.assertEqual((redesign.concepts, redesign.refinements), (2, 0))
        self.assertTrue(transition(ExplorationState(concepts=3, refinements=4, cycles=2), AuditDecision(DecisionAction.REFINE, "cap")).finished)
        self.assertTrue(transition(ExplorationState(), AuditDecision(DecisionAction.ACCEPT, "verified")).finished)


if __name__ == "__main__":
    unittest.main()
