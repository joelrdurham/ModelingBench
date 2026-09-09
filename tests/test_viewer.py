"""Browser regressions for independent evidence navigation and review context."""
import threading
import unittest
from pathlib import Path

from modelbench.server import ReviewServer
from tests.helpers import TemporaryProject


class ViewerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest('Install the test extra for browser checks')
        edge = Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')
        if not edge.exists():
            raise unittest.SkipTest('Microsoft Edge is unavailable')
        cls.project = TemporaryProject()
        cls.server = ReviewServer(cls.project.root, 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(executable_path=str(edge), headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.project.cleanup()

    def setUp(self):
        self.page = self.browser.new_page(viewport={'width': 1440, 'height': 1000})
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        assets = []
        for revision in ['revision_000001', 'revision_000002']:
            for mode in ['final', 'motion']:
                for frame in range(3):
                    name = f'front_{frame:04d}.png'
                    assets.append({'id': f'{revision}-{mode}-{frame}', 'revision': revision,
                        'mode': mode, 'kind': mode, 'camera': 'front', 'frame': frame,
                        'name': name, 'alias': f'{revision}/{mode}/{name}', 'url': f'/fixture/{revision}-{mode}-{frame}.jpg'})
        self.record = {'run': 'sample/run_000001', 'state': 'awaiting_feedback', 'assets': assets,
            'references': [{'id': f'ref-{i}', 'name': f'reference-{i}.jpg', 'title': f'Reference {i}',
                'category': 'drawing' if i == 2 else 'reference', 'url': f'/fixture/ref-{i}.jpg'} for i in range(3)],
            'models': {'profiles': {'builder': {'model': 'pending-model', 'effort': 'low'}}},
            'provenance': {
                'revision_000001': {'builder': {'requested_model': 'older-model', 'requested_effort': 'medium'}},
                'revision_000002': {'builder': {'observed_model': 'evidence-model', 'observed_effort': 'high'},
                    'verifier': {'requested_model': 'verifier-model', 'requested_effort': 'low'}}},
            'review': {'revisions': [{'id': 'revision_000001'}, {'id': 'revision_000002'}],
                'tasks': [{'status': 'open', 'requirement': 'Check silhouette', 'instruction': 'Inspect shape',
                    'evidence': [{'path': 'revisions/revision_000001/verification/inputs/motion/front_0001.png#check-id'}]}]}}
        self.data = {'runs': [self.record], 'tasks': ['sample'], 'workers': []}
        self.page.route('**/api/status', lambda route: route.fulfill(json=self.data))
        image = self.project.root / 'tasks/goodyear_optitrac_lsw_1400_30r46/inputs/manufacturer_optitrac_product_line.jpg'
        self.page.route('**/fixture/*', lambda route: route.fulfill(path=str(image), content_type='image/jpeg'))
        self.page.goto(self.server.origin)
        self.page.locator('.hero').wait_for()

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.page.close()

    def count(self, panel):
        return self.page.locator(f'[data-panel="{panel}"] .image-count').inner_text()

    def test_hover_navigation_wraps_independently_within_category(self):
        self.page.locator('.hero').hover()
        self.page.keyboard.press('ArrowLeft')
        self.assertIn('(3/3)', self.count('render'))
        self.assertIn('(1/2)', self.count('reference'))
        self.page.keyboard.press('ArrowRight')
        self.assertIn('(1/3)', self.count('render'))
        self.page.locator('.reference-image').hover()
        self.page.keyboard.press('ArrowRight')
        self.assertIn('(2/2)', self.count('reference'))
        self.page.keyboard.press('ArrowRight')
        self.assertIn('(1/2)', self.count('reference'))
        self.assertIn('(1/3)', self.count('render'))
        self.page.get_by_label('Reference category', exact=True).select_option('drawing')
        self.assertIn('(1/1)', self.count('reference'))
        self.page.locator('.reference-image').hover()
        self.page.keyboard.press('ArrowRight')
        self.assertIn('(1/1)', self.count('reference'))
        self.page.get_by_label('Render category', exact=True).select_option('motion')
        self.page.mouse.move(0, 0)
        self.page.locator('[data-panel="render"]').focus()
        self.page.keyboard.press('ArrowRight')
        self.assertIn('motion-1', self.page.locator('.hero').get_attribute('src'))

    def test_model_provenance_changes_with_revision_and_setup_is_separate(self):
        self.assertIn('evidence-model', self.page.locator('.provenance').inner_text())
        self.assertIn('high effort', self.page.locator('.provenance').inner_text())
        self.assertNotIn('pending-model', self.page.locator('.provenance').inner_text())
        self.page.get_by_label('Revision', exact=True).select_option('revision_000001')
        self.assertIn('older-model', self.page.locator('.provenance').inner_text())
        self.assertIn('medium effort', self.page.locator('.provenance').inner_text())
        self.assertIn('Not recorded', self.page.locator('.provenance').inner_text())
        self.assertEqual(self.page.get_by_role('button', name='Start run', exact=True).count(), 0)
        self.page.get_by_role('button', name='New run', exact=True).click()
        self.assertTrue(self.page.get_by_role('button', name='Start run', exact=True).is_visible())
        self.assertEqual(self.page.locator('.viewer').count(), 0)
        self.assertEqual(self.page.get_by_role('button', name='Send feedback').count(), 0)
        self.page.get_by_role('button', name='Review runs', exact=True).click()
        self.assertEqual(self.page.get_by_label('Revision', exact=True).input_value(), 'revision_000001')

    def test_finding_jump_playback_and_keyboard_do_not_disrupt_inputs(self):
        self.assertEqual(self.page.locator('.ledger .section-heading p').evaluate('(node) => getComputedStyle(node).color'), 'rgb(227, 187, 124)')
        self.assertEqual(self.page.locator('.finding.unresolved summary').evaluate('(node) => getComputedStyle(node).color'), 'rgb(227, 187, 124)')
        self.page.locator('summary').filter(has_text='Check silhouette').click()
        self.assertEqual(self.page.get_by_role('button', name='View evidence').count(), 1)
        self.page.get_by_role('button', name='View evidence').click()
        self.assertEqual(self.page.get_by_label('Revision', exact=True).input_value(), 'revision_000001')
        self.assertIn('motion-1', self.page.locator('.hero').get_attribute('src'))
        feedback = self.page.get_by_placeholder('Describe input propagation')
        feedback.fill('inputs arrived')
        self.page.keyboard.press('ArrowLeft')
        self.assertIn('motion-1', self.page.locator('.hero').get_attribute('src'))
        self.page.locator('.hero').click()
        self.assertTrue(self.page.locator('dialog').is_visible())
        self.page.keyboard.press('ArrowRight')
        self.assertIn('motion-1', self.page.locator('.hero').get_attribute('src'))
        self.page.keyboard.press('Escape')
        self.page.locator('dialog').wait_for(state='detached')
        self.assertEqual(self.page.locator('dialog').count(), 0)
        self.page.get_by_label('Render frame', exact=True).fill('0')
        self.page.get_by_role('button', name='Play', exact=True).click()
        self.page.wait_for_function("document.querySelector('.hero').getAttribute('src').includes('motion-1')")
        self.page.get_by_role('button', name='Stop', exact=True).click()
        self.assertIn('(2/3)', self.count('render'))

    def test_new_run_submits_selected_effort_for_each_role(self):
        requests = []
        self.page.route('**/api/action', lambda route: (requests.append(route.request.post_data_json), route.fulfill(json={'accepted': True})))
        self.page.get_by_role('button', name='New run', exact=True).click()
        self.assertEqual(self.page.get_by_label('Builder effort', exact=True).input_value(), '')
        self.page.get_by_label('Builder effort', exact=True).select_option('ultra')
        self.page.get_by_label('Verifier effort', exact=True).select_option('medium')
        self.page.get_by_role('button', name='Start run', exact=True).click()
        self.page.locator('.success').wait_for()
        self.assertEqual(requests, [{'action': 'run', 'task': 'sample', 'agent': 'codex', 'verifier': 'codex', 'builder_effort': 'ultra', 'verifier_effort': 'medium'}])
        self.page.get_by_role('button', name='Start run', exact=True).click()
        self.page.wait_for_function("document.querySelector('.success') !== null")
        self.assertNotIn('builder_effort', requests[-1])
        self.assertNotIn('verifier_effort', requests[-1])

    def test_empty_categories_and_mobile_layout(self):
        self.record['assets'] = []
        self.record['references'] = []
        self.record['provenance'] = {}
        self.page.get_by_role('button', name='Refresh', exact=True).click()
        self.page.wait_for_function("document.querySelector('.hero').hidden")
        self.assertEqual(self.count('render'), '0 images')
        self.assertEqual(self.count('reference'), '0 images')
        self.page.locator('[data-panel="reference"]').focus()
        self.page.keyboard.press('ArrowRight')
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
        left = self.page.locator('[data-panel="render"]').bounding_box()
        right = self.page.locator('[data-panel="reference"]').bounding_box()
        self.assertGreater(right['y'], left['y'] + left['height'])

    def test_historical_model_context_is_distinct_from_revision_provenance(self):
        self.record['provenance']['revision_000002'] = {'builder': None,
            'builder_context': {'scope': 'run', 'invocation': {
                'requested_model': 'historical-model', 'requested_effort': 'high'}}}
        self.page.get_by_role('button', name='Refresh', exact=True).click()
        self.page.wait_for_function("document.querySelector('.provenance').textContent.includes('historical-model')")
        badges = self.page.locator('.provenance').inner_text()
        self.assertIn('RUN BUILDER', badges)
        self.assertIn('high effort', badges)
        self.assertIn('revision attribution unavailable', badges)
        self.assertNotIn('PRODUCED BY', badges)
        self.assertNotIn('pending-model', badges)


if __name__ == '__main__':
    unittest.main()
