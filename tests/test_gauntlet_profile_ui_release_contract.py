"""Release contracts for the bundled profile wizard and overview.

Structural regression coverage only: these do not claim browser interaction,
backend inference, active-runtime identity, installation or release acceptance.
"""
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
import unittest
from urllib.parse import urlsplit


WEB = Path(__file__).resolve().parents[1] / 'hermes_nerve' / 'web'


class PanelMarkup(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.ids = []
        self.steps = []
        self.assets = []
        self.text = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.ids.append(attrs['id'])
        if 'data-step' in attrs:
            self.steps.append((tag, attrs['data-step']))
        if tag == 'script' and attrs.get('src'):
            self.assets.append(attrs['src'])
        if tag == 'link' and attrs.get('rel') == 'stylesheet':
            self.assets.append(attrs['href'])

    def handle_data(self, data):
        self.text.append(data)


def validate_panel(markup):
    """Reject an incomplete or misleading bundled configuration overview."""
    panel = PanelMarkup(markup)
    if panel.steps != [('button', str(n)) for n in range(4)]:
        raise ValueError('exactly four ordered setup-step buttons required')
    counts = Counter(panel.ids)
    required = {
        'steps', 'content', 'back', 'next', 'messages', 'progress',
        'readout-profile', 'readout-modules', 'readout-backend', 'readout-dirty',
        'hermes-health', 'plugin-health', 'runtime-identity',
    }
    if any(counts[name] != 1 for name in required):
        raise ValueError('wizard and overview controls must exist exactly once')
    if any(count > 1 for count in counts.values()):
        raise ValueError('duplicate DOM identifiers')
    text = ' '.join(panel.text).casefold()
    if 'not live telemetry' not in text or 'not verified' not in text:
        raise ValueError('configuration overview must not imply verified live telemetry')
    if not panel.assets or any(urlsplit(asset).netloc or not asset.startswith('/assets/')
                               or '..' in asset.split('/') for asset in panel.assets):
        raise ValueError('use bundled local assets, not remote frontend dependencies')
    return panel


class TestProfileUiReleaseContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.markup = (WEB / 'index.html').read_text(encoding='utf-8')

    def test_shipped_wizard_and_overview_contract(self):
        panel = validate_panel(self.markup)
        for asset in panel.assets:
            with self.subTest(asset=asset):
                target = WEB / asset.removeprefix('/assets/')
                self.assertTrue(target.is_file(), f'missing bundled asset {asset}')
                self.assertGreater(target.stat().st_size, 0)

    def test_full_configuration_name_agrees_with_cli(self):
        import tempfile
        from hermes_nerve.control_panel import ControlPanel
        with tempfile.TemporaryDirectory(prefix='nerve-profile-label-') as folder:
            panel = ControlPanel(home=Path(folder), source=WEB.parent.parent)
            names = {p['id']: p['name'] for p in panel.status()['profiles']}
        self.assertEqual(names['custom'], 'Full Configuration')

    def test_rollback_reloads_complete_profile_state_and_preview(self):
        source = (WEB / 'app.js').read_text(encoding='utf-8')
        rollback = source.split('const done = await api("/api/rollback",', 1)[1].split('catch(error)', 1)[0]
        self.assertIn('await refreshStatus();', rollback)
        self.assertNotIn('state.info = await api("/api/status")', rollback)

    def test_missing_review_step_is_rejected(self):
        self.assertIn('data-step="3"', self.markup)
        with self.assertRaisesRegex(ValueError, 'four ordered'):
            validate_panel(self.markup.replace('data-step="3"', 'data-step="2"'))

    def test_missing_profile_readout_is_rejected(self):
        self.assertIn('id="readout-profile"', self.markup)
        with self.assertRaisesRegex(ValueError, 'exactly once'):
            validate_panel(self.markup.replace('id="readout-profile"', 'id="lost-profile"'))

    def test_live_telemetry_misrepresentation_is_rejected(self):
        self.assertIn('NOT LIVE TELEMETRY', self.markup)
        with self.assertRaisesRegex(ValueError, 'live telemetry'):
            validate_panel(self.markup.replace('NOT LIVE TELEMETRY', 'LIVE TELEMETRY'))

    def test_duplicate_overview_identifier_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'exactly once|duplicate'):
            validate_panel(self.markup + '<span id="readout-profile"></span>')

    def test_remote_frontend_dependency_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'bundled local assets'):
            validate_panel(self.markup + '<script src="https://example.invalid/ui.js"></script>')


if __name__ == '__main__':
    unittest.main()
