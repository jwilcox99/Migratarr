"""audit_overrides.py reports the configured override tags, not a hardcoded prefix."""

from contextlib import redirect_stdout
import importlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from planner_settings import load_settings, parse_settings
from runtime_config import load_config


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = load_config(ROOT / 'config/runtime.example.json', environ={})


def settings_with(overrides):
    data = json.loads((ROOT / 'config/planner.example.json').read_text(encoding='utf-8'))
    if overrides is not None:
        data['overrides'] = overrides
    return parse_settings(data)


def import_audit(settings):
    # Importing must not read credentials or call Radarr/Sonarr.
    with patch('runtime_config.get_config', return_value=RUNTIME), \
            patch('planner_settings.get_settings', return_value=settings), \
            patch('service_keys.service_endpoint', side_effect=AssertionError('credential read at import')), \
            patch('urllib.request.urlopen', side_effect=AssertionError('HTTP at import')):
        return importlib.reload(importlib.import_module('audit_overrides'))


TAGS = [{'id': 1, 'label': 'Keep'}, {'id': 2, 'label': 'vault'}, {'id': 3, 'label': 'migratarr-lock'},
        {'id': 4, 'label': 'migratarr-rare'}, {'id': 5, 'label': 'migratarr-ssh-old'}, {'id': 6, 'label': '4k'}]
MEDIA = [{'title': 'Locked Film', 'tags': [1, 6]}, {'title': 'Vault Film', 'tags': [2]},
         {'title': 'Legacy Film', 'tags': [3, 4]}, {'title': 'Plain Film', 'tags': [6]}]


class AuditOverridesTests(unittest.TestCase):
    def run_audit(self, settings, tags=TAGS, media=MEDIA):
        module = import_audit(settings)
        calls = []

        def get_json(url, key):
            calls.append((url, key))
            return tags if url.endswith('/tag') else media

        out = io.StringIO()
        with patch.object(module, 'get_json', side_effect=get_json), \
                patch.object(module, 'service_endpoint',
                             side_effect=lambda service, runtime: (RUNTIME.urls[service], service + '-key')), \
                redirect_stdout(out):
            module.main()
        return out.getvalue(), calls

    def test_custom_tags_reported_and_leftover_defaults_flagged(self):
        output, calls = self.run_audit(settings_with({'lock_tag': 'keep',
                                                     'category_tags': {'vault': 'Rare', 'shelf': 'Library'}}))
        radarr = output.split('SONARR')[0]
        self.assertIn('  1: Keep (lock)', radarr)
        self.assertIn('  2: vault (category Rare)', radarr)
        stray = radarr.split('Unrecognized migratarr-* tags')[1].split('Tagged media:')[0]
        self.assertEqual([l.strip() for l in stray.splitlines()[1:] if l.strip()],
                         ['3: migratarr-lock', '4: migratarr-rare', '5: migratarr-ssh-old'])
        self.assertIn('Locked Film | Keep\n', radarr)
        self.assertIn('Vault Film | vault\n', radarr)
        self.assertIn('Legacy Film | migratarr-lock (unrecognized), migratarr-rare (unrecognized)\n', radarr)
        self.assertNotIn('Plain Film', radarr)
        self.assertNotIn('4k', radarr)
        # Only GETs of the tag and media lists, with each service's own key.
        base = {'radarr': RUNTIME.urls['radarr'], 'sonarr': RUNTIME.urls['sonarr']}
        self.assertEqual(calls, [(base['radarr'] + '/api/v3/tag', 'radarr-key'),
                                 (base['radarr'] + '/api/v3/movie', 'radarr-key'),
                                 (base['sonarr'] + '/api/v3/tag', 'sonarr-key'),
                                 (base['sonarr'] + '/api/v3/series', 'sonarr-key')])

    def test_default_tags_match_former_prefix_report(self):
        output, _ = self.run_audit(load_settings(ROOT / 'config/planner.example.json'))
        radarr = output.split('SONARR')[0]
        self.assertIn('  3: migratarr-lock (lock)', radarr)
        self.assertIn('  4: migratarr-rare (category Rare)', radarr)
        self.assertIn('  5: migratarr-ssh-old', radarr.split('Unrecognized')[1])
        self.assertNotIn('Keep', radarr)
        self.assertIn('Legacy Film | migratarr-lock, migratarr-rare\n', radarr)
        self.assertNotIn('Vault Film', radarr)

    def test_no_override_tags(self):
        output, calls = self.run_audit(settings_with({'lock_tag': 'keep', 'category_tags': {'vault': 'Rare'}}),
                                       tags=[{'id': 6, 'label': '4k'}])
        self.assertEqual(output.count('No Migratarr override tags currently exist.'), 2)
        self.assertTrue(all(url.endswith('/tag') for url, _ in calls))


if __name__ == '__main__':
    unittest.main()
