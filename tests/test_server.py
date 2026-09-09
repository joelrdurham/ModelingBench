from __future__ import annotations

import base64
import http.client
import json
import threading
import unittest
from pathlib import Path

from modelbench.server import ReviewServer
from modelbench.util import atomic_write_json, sha256_file
from tests.helpers import TemporaryProject


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.project = TemporaryProject()
        self.server = ReviewServer(self.project.root, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.project.cleanup()

    def request(self, method, path, *, headers=None, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        connection.request(method, path, body=body, headers={'Host': f'127.0.0.1:{self.server.server_port}', **(headers or {})})
        response = connection.getresponse(); data = response.read(); connection.close(); return response, data

    def test_rejects_dns_rebinding_host_and_unauthorized_post(self):
        response, _ = self.request('GET', '/', headers={'Host':'evil.test'})
        self.assertEqual(response.status, 403)
        response, _ = self.request('POST', '/api/action', headers={'Content-Type':'application/json'}, body=b'{}')
        self.assertEqual(response.status, 403)

    def test_action_routes_only_require_their_own_fields(self):
        from unittest.mock import Mock
        self.server.start_worker = Mock(return_value={"accepted": True, "pid": 7})
        headers = {"Origin": self.server.origin, "X-ModelBench-Action": self.server.action_token, "Content-Type": "application/json"}
        for action in ("pause", "resume", "cancel", "revise", "verify"):
            response, _ = self.request("POST", "/api/action", headers=headers, body=json.dumps({"action": action, "run": "sample/run_000001"}).encode())
            self.assertEqual(response.status, 202)
        response, _ = self.request("POST", "/api/action", headers=headers, body=json.dumps({"action": "resume", "run": "sample/run_000001", "additional_budget_seconds": 30}).encode())
        self.assertEqual(response.status, 202)
        self.assertEqual(self.server.start_worker.call_args.args[0], ["resume", "sample/run_000001", "--additional-budget-seconds", "30"])
        response, _ = self.request("POST", "/api/action", headers=headers, body=json.dumps({"action": "resume", "run": "sample/run_000001", "additional_budget_seconds": -1}).encode())
        self.assertEqual(response.status, 400)
        response, _ = self.request("POST", "/api/not-action", headers=headers, body=b"{}")
        self.assertEqual(response.status, 404)
        response, _ = self.request("POST", "/api/action", headers=headers, body=b"{")
        self.assertEqual(response.status, 400)
    def test_new_run_passes_optional_effort_overrides(self):
        from unittest.mock import Mock
        self.server.start_worker = Mock(return_value={'accepted': True})
        headers = {'Origin': self.server.origin, 'X-ModelBench-Action': self.server.action_token, 'Content-Type': 'application/json'}
        request = {'action': 'run', 'task': 'sample', 'agent': 'codex', 'verifier': 'codex'}
        base = ['run', 'sample', '--agent', 'codex', '--verifier', 'codex']
        for overrides, flags in [({}, []), ({'builder_effort': 'ultra', 'verifier_effort': 'medium'}, ['--builder-effort', 'ultra', '--verifier-effort', 'medium'])]:
            response, _ = self.request('POST', '/api/action', headers=headers, body=json.dumps({**request, **overrides}).encode())
            self.assertEqual(response.status, 202)
            self.assertEqual(self.server.start_worker.call_args.args[0], base + flags)
        self.server.start_worker.reset_mock()
        response, _ = self.request('POST', '/api/action', headers=headers, body=json.dumps({**request, 'builder_effort': 'invalid'}).encode())
        self.assertEqual(response.status, 400)
        self.server.start_worker.assert_not_called()

    def test_system_feedback_is_global_and_does_not_start_a_worker_or_mutate_review(self):
        from unittest.mock import Mock
        run = self.server.generated / "sample" / "runs" / "run_000001"
        atomic_write_json(run / "state.json", {"task_id": "sample", "run_id": "run_000001", "state": "awaiting_feedback"})
        review = {"revisions": [], "tasks": [{"id": "keep"}]}
        atomic_write_json(run / "review.json", review)
        self.server.start_worker = Mock()
        headers = {"Origin": self.server.origin, "X-ModelBench-Action": self.server.action_token, "Content-Type": "application/json"}
        response, raw = self.request("POST", "/api/action", headers=headers, body=json.dumps({"action": "system-feedback", "run": "sample/run_000001", "message": "The drawing input arrived correctly."}).encode())
        self.assertEqual(response.status, 202)
        self.assertTrue(json.loads(raw)["id"].startswith("sfb_"))
        events = [json.loads(line) for line in (self.server.generated / "system-feedback" / "events.jsonl").read_text().splitlines()]
        self.assertEqual(len(events), 1)
        self.assertEqual({key: events[0][key] for key in ("run_id", "message")}, {"run_id": "sample/run_000001", "message": "The drawing input arrived correctly."})
        self.assertIn("timestamp", events[0])
        self.assertEqual(json.loads((run / "review.json").read_text()), review)
        self.server.start_worker.assert_not_called()
        atomic_write_json(run / "state.json", {"task_id": "sample", "run_id": "run_000001", "state": "completed"})
        response, _ = self.request("POST", "/api/action", headers=headers, body=json.dumps({"action": "system-feedback", "run": "sample/run_000001", "message": "late"}).encode())
        self.assertEqual(response.status, 400)

    def test_only_registered_intact_image_evidence_is_exposed(self):
        run = self.server.generated / 'sample' / 'runs' / 'run_000001'
        (run / 'evidence').mkdir(parents=True)
        image = run / 'evidence' / 'front_0040.png'; image.write_bytes(b'png')
        secret = run / 'secret.png'; secret.write_bytes(b'secret')
        atomic_write_json(run / 'state.json', {'task_id':'sample','run_id':'run_000001','state':'awaiting_feedback'})
        atomic_write_json(run / 'review.json', {'revisions':[{'id':'revision_000001','evidence':[
            {'path':'evidence/front_0040.png','sha256':sha256_file(image),'kind':'final'},
            {'path':'secret.png','sha256':'wrong','kind':'final'}]}]})
        response, raw = self.request('GET', '/api/status')
        self.assertEqual(response.status, 200); assets=json.loads(raw)['runs'][0]['assets']; self.assertEqual(len(assets), 1); self.assertEqual((assets[0]['camera'], assets[0]['frame'], assets[0]['mode']), ('front', 40, 'final'))
        response, data = self.request('GET', assets[0]['url']); self.assertEqual((response.status,data), (200,b'png'))
        response, _ = self.request('GET', '/api/assets/not-a-real-id'); self.assertEqual(response.status, 404)

    def test_reference_images_and_verifier_provenance_are_hash_confined(self):
        run = self.server.generated / 'sample' / 'runs' / 'run_000001'
        inputs = run / 'snapshot' / 'task' / 'inputs'; inputs.mkdir(parents=True)
        image = inputs / 'reference.png'; image.write_bytes(b'reference')
        secret = run / 'snapshot' / 'task' / 'secret.png'; secret.write_bytes(b'secret')
        receipt = run / 'revisions' / 'revision_000001' / 'verification' / 'invocation.json'; receipt.parent.mkdir(parents=True)
        atomic_write_json(receipt, {'settings': {'requested_model': 'gpt-test', 'requested_effort': 'high'}})
        atomic_write_json(run / 'state.json', {'task_id':'sample','run_id':'run_000001','state':'awaiting_feedback'})
        atomic_write_json(run / 'run.json', {'input_manifest': {'inputs': [
            {'id':'front_reference','role':'reference_image','path':'reference.png','title':'Front reference'},
            {'id':'escape','role':'reference_image','path':'../secret.png'},
        ]}, 'task': {'files': [{'path':'inputs/reference.png','sha256':sha256_file(image)}]}})
        atomic_write_json(run / 'review.json', {'revisions':[{'id':'revision_000001','sha256':'artifact-hash','builder_provenance':{'requested_model':'builder-test','requested_effort':'high','artifact_sha256':'artifact-hash'},'evidence':[{'path':receipt.relative_to(run).as_posix(), 'sha256':sha256_file(receipt)}]}]})
        response, raw = self.request('GET', '/api/status'); self.assertEqual(response.status, 200)
        record = json.loads(raw)['runs'][0]
        self.assertEqual([{key: item[key] for key in ('name', 'title', 'category', 'notes', 'alias')} for item in record['references']], [{'name':'reference.png', 'title':'Front reference', 'category':'reference', 'notes':None, 'alias':'reference.png'}])
        response, data = self.request('GET', record['references'][0]['url']); self.assertEqual((response.status, data), (200, b'reference'))
        self.assertEqual(record['provenance']['revision_000001']['builder']['requested_model'], 'builder-test')
        self.assertEqual(record['provenance']['revision_000001']['verifier']['requested_model'], 'gpt-test')
        image.write_bytes(b'tampered')
        response, raw = self.request('GET', '/api/status'); self.assertEqual(json.loads(raw)['runs'][0]['references'], [])
        response, _ = self.request('GET', record['references'][0]['url']); self.assertEqual(response.status, 404)
    def test_browser_viewer_selects_motion_frames_and_submits_feedback(self):
        from unittest.mock import Mock
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Install the test extra for browser checks")
        edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
        if not edge.exists():
            self.skipTest("Microsoft Edge is unavailable")
        run = self.server.generated / "sample" / "runs" / "run_000001"
        evidence = run / "evidence"; evidence.mkdir(parents=True)
        revisions = []
        for number in (1, 2):
            records = []
            for frame in range(80):
                image = evidence / f"rev_{number}" / f"front_{frame:04d}.png"
                image.parent.mkdir(parents=True, exist_ok=True)
                image.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL9WQAAAABJRU5ErkJggg=="))
                records.append({"path": image.relative_to(run).as_posix(), "sha256": sha256_file(image), "kind": "motion"})
            revisions.append({"id": f"revision_{number:06d}", "evidence": records})
        atomic_write_json(run / "state.json", {"task_id": "sample", "run_id": "run_000001", "state": "awaiting_feedback"})
        atomic_write_json(run / "review.json", {"revisions": revisions, "tasks": [{"id": "f", "status": "open", "requirement": "motion", "instruction": "check frame", "evidence": [{"path": "evidence/rev_2/front_0040.png"}], "history": [], "responses": []}]})
        self.server.start_worker = Mock(return_value={"accepted": True, "pid": 3})
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
            page = browser.new_page()
            page.goto(f"{self.server.origin}/?action={self.server.action_token}")
            page.wait_for_selector(".hero")
            self.assertIn("frame 0 (1/80)", page.locator("[data-panel=\"render\"] .image-count").inner_text())
            page.locator("input[type=range]").fill("40")
            self.assertIn("frame 40", page.locator("[data-panel=\"render\"] .image-count").inner_text())
            page.locator(".finding summary").filter(has_text="motion").click(); page.get_by_role("button", name="View evidence").click()
            self.assertIn("frame 40", page.locator("[data-panel=\"render\"] .image-count").inner_text())
            page.locator("textarea[placeholder='Describe input propagation']").fill("inputs arrived")
            page.get_by_text("Submit system feedback").click()
            page.wait_for_timeout(50)
            self.assertEqual(self.server.start_worker.call_count, 0)
            self.assertTrue((self.server.generated / "system-feedback" / "events.jsonl").is_file())
            browser.close()

    def test_browser_renders_discovery_ledger_and_budget_control(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Install the test extra for browser checks")
        edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
        if not edge.exists():
            self.skipTest("Microsoft Edge is unavailable")
        run = self.server.generated / "sample" / "runs" / "run_000001"
        (run / "snapshot").mkdir(parents=True)
        (run / "discovery" / "versions" / "spec_000001").mkdir(parents=True)
        spec = {"target":{"identity":"Field lantern","scope":"full asset","status":"established"},
                "entities":[{"id":"body","name":"Lamp body"}],
                "claims":[{"id":"c1","kind":"prior","statement":"Hidden rear panel is inferred"}],
                "requirements":[{"id":"r1","critical":True,"criterion":"Body silhouette matches"}],
                "references":[{"id":"ref1","applicability":"Front reference"}]}
        atomic_write_json(run / "snapshot" / "task.json", {"specification_mode":"discovered"})
        atomic_write_json(run / "discovery" / "versions" / "spec_000001" / "specification.json", spec)
        atomic_write_json(run / "discovery" / "ledger.json", {"version":1, "active":"spec_000001", "stage":"modeling", "working_seed":"candidate_2",
            "candidates":[{"id":"candidate_2","selected":True}], "versions":[{"id":"spec_000001","specification":{"path":"discovery/versions/spec_000001/specification.json"}}],
            "last_audit":{"decision":"APPROVE","assessment":"Coverage complete"}})
        atomic_write_json(run / "state.json", {"task_id":"sample","run_id":"run_000001","state":"modeling"})
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
            page = browser.new_page(); page.goto(f"{self.server.origin}/?action={self.server.action_token}")
            page.get_by_text("Field lantern").wait_for()
            self.assertTrue(page.get_by_text("Field lantern").is_visible())
            self.assertTrue(page.get_by_text("[prior] Hidden rear panel is inferred").is_visible())
            self.assertTrue(page.get_by_text("working seed: candidate_2").is_visible())
            page.get_by_role("button", name="New run").click()
            self.assertTrue(page.get_by_text("Budget seconds").is_visible())
            browser.close()

if __name__ == '__main__': unittest.main()
