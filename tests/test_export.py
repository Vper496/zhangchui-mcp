from copy import deepcopy
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from zhangchui_mcp.client import ClientConfig, QueryError
from zhangchui_mcp.export import download, verify, recalculate, main


RELEASE, REVISION = 'a' * 64, 'b' * 64


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def fixture():
    rows = {
        'reports': [{'report_id': 'c' * 64}],
        'faction_statistics': [
            {'report_id': 'c' * 64, 'report_source': 'palm', 'faction_id': 'a', 'wins': 1, 'losses': 0, 'draws': 2,
             'win_rate_denominator': 1, 'win_rate': 1.0},
            {'report_id': 'd' * 64, 'report_source': 'wf', 'faction_id': 'a', 'wins': None, 'losses': None, 'draws': None,
             'win_rate_denominator': None, 'win_rate': .55},
            {'report_id': 'e' * 64, 'report_source': 'palm', 'faction_id': 'a', 'wins': 0, 'losses': 0, 'draws': 2,
             'win_rate_denominator': 0, 'win_rate': None}],
        'matchup_statistics': [],
    }
    tables, pages = {}, {}
    for name, items in rows.items():
        raw = b''.join(canonical(row) + b'\n' for row in items)
        descriptor = {'sha256': hashlib.sha256(canonical({'items': items})).hexdigest(),
                      'jsonl_sha256': hashlib.sha256(raw).hexdigest(), 'jsonl_bytes': len(raw), 'rows': len(items)}
        key = ['report_id'] if name == 'reports' else ['report_id', 'faction_id']
        if name == 'matchup_statistics':
            key.append('opponent_id')
        tables[name] = {'row_count': len(items), 'jsonl_bytes': len(raw),
                        'jsonl_sha256': hashlib.sha256(raw).hexdigest(),
                        'pages': [descriptor] if items else [],
                        'primary_key': key, 'fields': {field: {} for field in (items[0] if items else key)}}
        pages[name] = {'data': {'table': name, 'items': items, 'count': len(items), 'page': 1,
                               'page_size': 200, 'has_next': False, 'chunk': descriptor},
                       'meta': {'release_id': RELEASE, 'dataset_revision': REVISION}}
    manifest = {'data': {'schema': 'public-query-dataset/1', 'release_id': RELEASE,
                         'dataset_revision': REVISION, 'tables': tables},
                'meta': {'release_id': RELEASE, 'dataset_revision': REVISION}}
    return manifest, pages


class FakeClient:
    def __init__(self, manifest=None, pages=None):
        default_manifest, default_pages = fixture()
        self.manifest, self.pages = manifest or default_manifest, pages or default_pages
        self.calls = []

    def query(self, resource, **parameters):
        self.calls.append((resource, parameters))
        if resource == 'dataset_manifest':
            return deepcopy(self.manifest)
        return deepcopy(self.pages[parameters['table']])


class ExportTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / 'export'

    def test_download_pins_revision_and_independent_offline_recalculation_uses_no_network(self):
        client = FakeClient()
        result = download(client, self.output)
        self.assertTrue(result['verified'])
        self.assertEqual(result['row_counts'], {'reports': 1, 'faction_statistics': 3, 'matchup_statistics': 0})
        for resource, parameters in client.calls[1:]:
            self.assertEqual(resource, 'dataset_rows')
            self.assertEqual(parameters['release_id'], RELEASE)
            self.assertEqual(parameters['dataset_revision'], REVISION)
        before = len(client.calls)
        self.assertTrue(verify(self.output)['verified'])
        independent = recalculate(self.output)
        self.assertEqual(independent['win_rate_rows_checked'], 2)
        self.assertEqual(independent['unavailable_rows'], 1)
        self.assertEqual(len(client.calls), before)
        self.assertIn('draws excluded', independent['formula'])

    def test_bad_page_hash_fails_without_exposing_a_partial_package(self):
        manifest, pages = fixture()
        pages['reports']['data']['items'][0]['report_id'] = 'changed'
        with self.assertRaises(ValueError):
            download(FakeClient(manifest, pages), self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_revision_change_mid_download_fails_and_does_not_mix_data(self):
        manifest, pages = fixture()
        pages['faction_statistics']['meta']['dataset_revision'] = 'f' * 64
        with self.assertRaises(ValueError):
            download(FakeClient(manifest, pages), self.output)
        self.assertFalse(self.output.exists())

    def test_manifest_body_and_envelope_revision_must_match_before_download(self):
        manifest, pages = fixture()
        manifest['data']['dataset_revision'] = 'f' * 64
        client = FakeClient(manifest, pages)
        with self.assertRaises(ValueError):
            download(client, self.output)
        self.assertEqual(len(client.calls), 1)
        self.assertFalse(self.output.exists())

    def test_offline_tamper_truncation_and_added_rows_fail(self):
        download(FakeClient(), self.output)
        file = self.output / 'faction_statistics.jsonl'
        original = file.read_bytes()
        for changed in (original.replace(b'1.0', b'0.5'), original[:-1], original + original.splitlines(True)[0]):
            file.write_bytes(changed)
            with self.assertRaises(ValueError):
                verify(self.output)
        file.write_bytes(original)
        self.assertTrue(verify(self.output)['verified'])

    def test_unknown_raw_tables_and_oversized_download_are_rejected_before_rows(self):
        for kind in ('unknown_table', 'oversize'):
            manifest, pages = fixture()
            if kind == 'unknown_table':
                manifest['data']['tables']['players'] = {}
            else:
                manifest['data']['tables']['reports']['jsonl_bytes'] = 129 * 1024 * 1024
            client = FakeClient(manifest, pages)
            with self.assertRaises(ValueError):
                download(client, self.output)
            self.assertEqual(len(client.calls), 1)

    def test_existing_folder_is_preserved(self):
        self.output.mkdir()
        file = self.output / 'keep'
        file.write_bytes(b'original')
        with self.assertRaises(ValueError):
            download(FakeClient(), self.output)
        self.assertEqual(file.read_bytes(), b'original')

    def test_download_cli_paces_default_quota_and_preserves_slower_user_setting(self):
        for interval, expected in ((2, 6.1), (12, 12)):
            config = ClientConfig(api_key='synthetic-export-key-only', minimum_interval_seconds=interval)
            with patch('sys.argv', ['zhangchui-export', 'download', str(self.output)]), \
                    patch('zhangchui_mcp.export.configuration_from_environment', return_value=config), \
                    patch('zhangchui_mcp.export.PublicQueryClient') as client, \
                    patch('zhangchui_mcp.export.download', return_value={'verified': True}), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)
                self.assertEqual(client.call_args.args[0].minimum_interval_seconds, expected)

    def test_cli_quota_failure_preserves_retry_after_and_explains_how_to_retry(self):
        output = io.StringIO()
        config = ClientConfig(api_key='synthetic-export-key-only')
        with patch('sys.argv', ['zhangchui-export', 'download', str(self.output)]), \
                patch('zhangchui_mcp.export.configuration_from_environment', return_value=config), \
                patch('zhangchui_mcp.export.download',
                      side_effect=QueryError('MCP_QUOTA_EXCEEDED', 'synthetic quota', status=429, retry_after=60)), \
                redirect_stdout(io.StringIO()), unittest.mock.patch('sys.stderr', output):
            self.assertEqual(main(), 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result['error']['retry_after'], 60)
        self.assertIn('ZHANGCHUI_MIN_INTERVAL_SECONDS', result['recovery'])

    def test_wrong_recomputable_statistics_fail_even_when_file_hashes_are_consistent(self):
        manifest, pages = fixture()
        pages['faction_statistics']['data']['items'][0]['win_rate'] = .75
        rows = pages['faction_statistics']['data']['items']
        raw = b''.join(canonical(row) + b'\n' for row in rows)
        table = manifest['data']['tables']['faction_statistics']
        descriptor = table['pages'][0]
        descriptor.update(sha256=hashlib.sha256(canonical({'items': rows})).hexdigest(),
                          jsonl_sha256=hashlib.sha256(raw).hexdigest(), jsonl_bytes=len(raw))
        table.update(jsonl_sha256=descriptor['jsonl_sha256'], jsonl_bytes=len(raw))
        pages['faction_statistics']['data']['chunk'] = descriptor
        download(FakeClient(manifest, pages), self.output)
        with self.assertRaisesRegex(ValueError, '离线复算'):
            recalculate(self.output)

    def test_independent_source_counts_and_rounded_proportions_remain_as_reported(self):
        for source, rate, denominator in [('wf', .33, 3), ('sns', .67, 2)]:
            manifest, pages = fixture()
            rows = pages['faction_statistics']['data']['items']
            rows[0].update(report_source=source, wins=1, losses=1, draws=1,
                           win_rate_denominator=denominator, win_rate=rate)
            raw = b''.join(canonical(row) + b'\n' for row in rows)
            table = manifest['data']['tables']['faction_statistics']
            descriptor = table['pages'][0]
            descriptor.update(sha256=hashlib.sha256(canonical({'items': rows})).hexdigest(),
                              jsonl_sha256=hashlib.sha256(raw).hexdigest(), jsonl_bytes=len(raw))
            table.update(jsonl_sha256=descriptor['jsonl_sha256'], jsonl_bytes=len(raw))
            pages['faction_statistics']['data']['chunk'] = descriptor
            output = self.root / source
            download(FakeClient(manifest, pages), output)
            result = recalculate(output)
            self.assertEqual(result['win_rate_rows_checked'], 1)
            self.assertEqual(result['unavailable_rows'], 2)
            self.assertEqual(result['source_methodology_rows'], 2)
            saved = json.loads((output / 'faction_statistics.jsonl').read_text().splitlines()[0])
            self.assertEqual(saved['win_rate'], rate)
            self.assertEqual(saved['win_rate_denominator'], denominator)


if __name__ == '__main__':
    unittest.main()
