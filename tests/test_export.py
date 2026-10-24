from copy import deepcopy
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from zhangchui_mcp.client import ClientConfig, QueryError
from zhangchui_mcp.export import (SCHEMA, GRANULAR_SCHEMA, _progress_directory,
                                  download, verify, recalculate, main)


RELEASE, REVISION = 'a' * 64, 'b' * 64


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def fixture(schema='public-query-dataset/1'):
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
    if schema == 'public-query-dataset/2':
        rows['reports'][0].update(granularity_available=True, granularity_unavailable_reason=None,
                                  detachments_available=True, detachments_unavailable_reason=None,
                                  roster_units_available=True, roster_units_unavailable_reason=None,
                                  context={'version_id': 'MFM 1.6'},
                                  coverage={'counts': {'records': 4}},
                                  detachments_coverage={'counts': {'detachment_missing': 1}},
                                  roster_units_coverage={'counts': {'analyzed_rosters': 4}},
                                  overlap={'available': True, 'reports': []})
        rows['detachment_statistics'] = [
            {'report_id': 'c' * 64, 'report_source': 'palm', 'format': 'individual', 'version_id': 'v1',
             'row_scope': 'faction', 'faction_id': 'tyranids', 'detachment_id': 'raw:' + '1' * 64,
             'label': 'Invasion Fleet', 'opponent_id': None, 'opponent_faction_id': None,
             'opponent_detachment_id': None, 'opponent_label': None, 'entries': 2,
             'usage_denominator': 4, 'usage': .5, 'wins': 1, 'losses': 1, 'draws': 0, 'games': 2,
             'win_rate_denominator': 2, 'win_rate': .5},
            {'report_id': 'c' * 64, 'report_source': 'wf', 'format': 'individual', 'version_id': 'v1',
             'row_scope': 'matchup', 'faction_id': 'tyranids', 'detachment_id': 'raw:' + '2' * 64,
             'label': 'Crusher Stampede | Talons of the Norn Queen', 'opponent_id': 'orks',
             'opponent_faction_id': 'orks', 'opponent_detachment_id': 'raw:' + '3' * 64,
             'opponent_label': 'Tide of Conviction', 'entries': None, 'usage_denominator': None,
             'usage': None, 'wins': None, 'losses': None, 'draws': None, 'games': None,
             'win_rate_denominator': None, 'win_rate': .55},
        ]
        rows['roster_unit_statistics'] = [
            {'report_id': 'c' * 64, 'report_source': 'palm', 'format': 'individual', 'version_id': 'v1',
             'faction_id': 'tyranids', 'unit_id': 'candidate:' + '4' * 64,
             'identity_status': 'candidate_only', 'identity_basis': 'name_match_candidate',
             'name_label': 'Tyrannofex', 'rosters': 2, 'usage_denominator': 4, 'usage': .5,
             'wins': 1, 'losses': 1, 'draws': 0, 'games': 2,
             'win_rate_denominator': 2, 'win_rate': .5,
             'comparison_available': True, 'comparison_basis': 'source_verified_bilateral_results',
             'comparison_unavailable_reason': None,
             'with_unit': {'games': 2, 'wins': 1, 'losses': 1, 'draws': 0,
                           'win_rate_denominator': 2, 'win_rate': .5},
             'without_unit': {'games': 2, 'wins': 2, 'losses': 0, 'draws': 0,
                              'win_rate_denominator': 2, 'win_rate': 1.0},
             'comparison_note': 'descriptive'},
            {'report_id': 'c' * 64, 'report_source': 'palm', 'format': 'individual', 'version_id': 'v1',
             'faction_id': 'tyranids', 'unit_id': 'unknown:' + '5' * 64,
             'identity_status': 'unknown', 'identity_basis': None, 'name_label': 'Unknown unit',
             'rosters': None, 'usage_denominator': None, 'usage': None,
             'wins': None, 'losses': None, 'draws': None, 'games': None,
             'win_rate_denominator': None, 'win_rate': None,
             'comparison_available': None, 'comparison_basis': None,
             'comparison_unavailable_reason': None, 'with_unit': None,
             'without_unit': None, 'comparison_note': None},
        ]
    tables, pages = {}, {}
    for name, items in rows.items():
        raw = b''.join(canonical(row) + b'\n' for row in items)
        descriptor = {'sha256': hashlib.sha256(canonical({'items': items})).hexdigest(),
                      'jsonl_sha256': hashlib.sha256(raw).hexdigest(), 'jsonl_bytes': len(raw), 'rows': len(items)}
        key = ['report_id'] if name == 'reports' else ['report_id', 'faction_id']
        if name == 'matchup_statistics':
            key.append('opponent_id')
        if name == 'detachment_statistics':
            key.extend(['row_scope', 'detachment_id', 'opponent_faction_id', 'opponent_detachment_id'])
        if name == 'roster_unit_statistics':
            key.append('unit_id')
        tables[name] = {'row_count': len(items), 'jsonl_bytes': len(raw),
                        'jsonl_sha256': hashlib.sha256(raw).hexdigest(),
                        'pages': [descriptor] if items else [],
                        'primary_key': key, 'fields': {field: {} for field in (items[0] if items else key)}}
        pages[name] = {'data': {'table': name, 'items': items, 'count': len(items), 'page': 1,
                               'page_size': 200, 'has_next': False, 'chunk': descriptor},
                       'meta': {'release_id': RELEASE, 'dataset_revision': REVISION}}
    manifest = {'data': {'schema': schema, 'release_id': RELEASE,
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
        key = (parameters['table'], parameters['page'])
        return deepcopy(self.pages[key] if key in self.pages else self.pages[parameters['table']])


def multipage_fixture():
    manifest, pages = fixture()
    name = 'faction_statistics'
    table = manifest['data']['tables'][name]
    original = pages.pop(name)
    items = original['data']['items']
    chunks = [items[:1], items[1:2], items[2:]]
    combined = b''.join(canonical(row) + b'\n' for row in items)
    table['jsonl_bytes'] = len(combined)
    table['jsonl_sha256'] = hashlib.sha256(combined).hexdigest()
    table['pages'] = []
    for number, chunk_rows in enumerate(chunks, 1):
        raw = b''.join(canonical(row) + b'\n' for row in chunk_rows)
        descriptor = {'sha256': hashlib.sha256(canonical({'items': chunk_rows})).hexdigest(),
                      'jsonl_sha256': hashlib.sha256(raw).hexdigest(),
                      'jsonl_bytes': len(raw), 'rows': len(chunk_rows)}
        table['pages'].append(descriptor)
        pages[(name, number)] = {
            'data': {'table': name, 'items': chunk_rows, 'count': table['row_count'], 'page': number,
                     'page_size': 1, 'has_next': number < len(chunks), 'chunk': descriptor},
            'meta': deepcopy(original['meta']),
        }
    return manifest, pages


class QuotaOnceClient(FakeClient):
    def __init__(self, manifest=None, pages=None, *, fail_at):
        super().__init__(manifest, pages)
        self.fail_at = fail_at
        self.failed = False

    def query(self, resource, **parameters):
        if (resource == 'dataset_rows'
                and (parameters['table'], parameters['page']) == self.fail_at
                and not self.failed):
            self.calls.append((resource, parameters))
            self.failed = True
            raise QueryError('MCP_QUOTA_EXCEEDED', 'synthetic quota', status=429, retry_after=60)
        return super().query(resource, **parameters)


class InterruptOnceClient(FakeClient):
    def __init__(self, manifest=None, pages=None, *, interrupt_at):
        super().__init__(manifest, pages)
        self.interrupt_at = interrupt_at
        self.interrupted = False

    def query(self, resource, **parameters):
        if (resource == 'dataset_rows'
                and (parameters['table'], parameters['page']) == self.interrupt_at
                and not self.interrupted):
            self.calls.append((resource, parameters))
            self.interrupted = True
            raise KeyboardInterrupt
        return super().query(resource, **parameters)


class RotatingQuotaClient(QuotaOnceClient):
    def __init__(self, original_manifest, newer_manifest, pages, *, fail_at):
        super().__init__(original_manifest, pages, fail_at=fail_at)
        self.latest_manifest = deepcopy(original_manifest)
        self.manifests = {
            (original_manifest['meta']['release_id'], original_manifest['meta']['dataset_revision']):
                deepcopy(original_manifest),
            (newer_manifest['meta']['release_id'], newer_manifest['meta']['dataset_revision']):
                deepcopy(newer_manifest),
        }

    def query(self, resource, **parameters):
        if resource == 'dataset_manifest':
            self.calls.append((resource, parameters))
            identity = parameters.get('release_id'), parameters.get('dataset_revision')
            if identity == (None, None):
                return deepcopy(self.latest_manifest)
            return deepcopy(self.manifests[identity])
        return super().query(resource, **parameters)


class ExportTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / 'export'

    def test_download_pins_revision_and_independent_offline_recalculation_uses_no_network(self):
        client = FakeClient()
        result = download(client, self.output, schema=SCHEMA)
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

    def test_v2_download_verifies_granular_rows_recalculates_and_keeps_unknown_null(self):
        manifest, pages = fixture(GRANULAR_SCHEMA)
        client = FakeClient(manifest, pages)
        result = download(client, self.output, schema=GRANULAR_SCHEMA)
        self.assertEqual(set(result['row_counts']), {'reports', 'faction_statistics', 'matchup_statistics',
                                                     'detachment_statistics', 'roster_unit_statistics'})
        for resource, parameters in client.calls[1:]:
            self.assertEqual(resource, 'dataset_rows')
            self.assertEqual(parameters['schema'], GRANULAR_SCHEMA)
            self.assertEqual(parameters['release_id'], RELEASE)
            self.assertEqual(parameters['dataset_revision'], REVISION)
        recalculated = recalculate(self.output)
        self.assertEqual(recalculated['detachment_win_rate_rows_checked'], 1)
        self.assertEqual(recalculated['detachment_usage_rows_checked'], 1)
        self.assertEqual(recalculated['roster_unit_win_rate_rows_checked'], 1)
        self.assertEqual(recalculated['roster_unit_usage_rows_checked'], 1)
        self.assertGreaterEqual(recalculated['granular_unavailable_rows'], 3)
        units = (self.output / 'roster_unit_statistics.jsonl').read_text(encoding='utf-8').splitlines()
        unknown = json.loads(units[1])
        self.assertEqual(unknown['identity_status'], 'unknown')
        self.assertIsNone(unknown['rosters'])
        self.assertIsNone(unknown['usage'])
        self.assertIsNone(unknown['wins'])

    def test_bad_page_hash_fails_without_exposing_a_partial_package(self):
        manifest, pages = fixture()
        pages['reports']['data']['items'][0]['report_id'] = 'changed'
        with self.assertRaises(ValueError):
            download(FakeClient(manifest, pages), self.output, schema=SCHEMA)
        self.assertFalse(self.output.exists())
        self.assertEqual(len(list(self.root.glob('.zhangchui-export-*'))), 1)

    def test_quota_failure_resumes_only_missing_pages_after_manifest_recheck(self):
        manifest, pages = multipage_fixture()
        client = QuotaOnceClient(manifest, pages, fail_at=('faction_statistics', 2))
        with self.assertRaises(QueryError):
            download(client, self.output, schema=SCHEMA)
        self.assertFalse(self.output.exists())
        stage = next(self.root.glob('.zhangchui-export-*'))
        self.assertTrue((stage / 'pages' / 'reports-00001.jsonl').is_file())
        self.assertTrue((stage / 'pages' / 'faction_statistics-00001.jsonl').is_file())

        result = download(client, self.output, schema=SCHEMA)
        self.assertTrue(result['verified'])
        self.assertTrue(self.output.is_dir())
        row_calls = [(parameters['table'], parameters['page'])
                     for resource, parameters in client.calls if resource == 'dataset_rows']
        self.assertEqual(row_calls.count(('reports', 1)), 1)
        self.assertEqual(row_calls.count(('faction_statistics', 1)), 1)
        self.assertEqual(row_calls.count(('faction_statistics', 2)), 2)
        self.assertEqual(row_calls.count(('faction_statistics', 3)), 1)
        self.assertEqual(sum(resource == 'dataset_manifest' for resource, _ in client.calls), 2)
        manifest_calls = [parameters for resource, parameters in client.calls
                          if resource == 'dataset_manifest']
        self.assertEqual(manifest_calls[0], {'schema': SCHEMA})
        self.assertEqual(manifest_calls[1], {'schema': SCHEMA, 'release_id': RELEASE,
                                             'dataset_revision': REVISION})
        self.assertEqual(list(self.root.iterdir()), [self.output])

    def test_corrupt_saved_page_stops_and_preserves_progress(self):
        manifest, pages = multipage_fixture()
        client = QuotaOnceClient(manifest, pages, fail_at=('faction_statistics', 2))
        with self.assertRaises(QueryError):
            download(client, self.output, schema=SCHEMA)
        stage = next(self.root.glob('.zhangchui-export-*'))
        saved_page = stage / 'pages' / 'reports-00001.jsonl'
        original = saved_page.read_bytes()
        saved_page.write_bytes(original[:-1] + b' ')
        calls_before = len(client.calls)
        with self.assertRaisesRegex(ValueError, '分片校验'):
            download(client, self.output, schema=SCHEMA)
        self.assertEqual(client.calls[calls_before:], [('dataset_manifest', {
            'schema': SCHEMA, 'release_id': RELEASE, 'dataset_revision': REVISION})])
        self.assertEqual(saved_page.read_bytes(), original[:-1] + b' ')
        self.assertFalse(self.output.exists())

    def test_resume_refuses_a_changed_manifest_and_keeps_old_pages(self):
        manifest, pages = multipage_fixture()
        client = QuotaOnceClient(manifest, pages, fail_at=('faction_statistics', 2))
        with self.assertRaises(QueryError):
            download(client, self.output, schema=SCHEMA)
        stage = next(self.root.glob('.zhangchui-export-*'))
        saved_page = stage / 'pages' / 'reports-00001.jsonl'
        original = saved_page.read_bytes()
        client.manifest['data']['dataset_revision'] = 'f' * 64
        client.manifest['meta']['dataset_revision'] = 'f' * 64
        calls_before = len(client.calls)
        with self.assertRaisesRegex(ValueError, 'manifest'):
            download(client, self.output, schema=SCHEMA)
        self.assertEqual(client.calls[calls_before:], [('dataset_manifest', {
            'schema': SCHEMA, 'release_id': RELEASE, 'dataset_revision': REVISION})])
        self.assertEqual(saved_page.read_bytes(), original)
        self.assertFalse(self.output.exists())

    def test_complete_pending_last_page_survives_interrupted_package_assembly(self):
        manifest, pages = multipage_fixture()
        client = QuotaOnceClient(manifest, pages, fail_at=('faction_statistics', 3))
        with self.assertRaises(QueryError):
            download(client, self.output, schema=SCHEMA)
        stage = _progress_directory(self.output)
        state_path = stage / 'progress.json'
        state = json.loads(state_path.read_text(encoding='utf-8'))
        response = client.pages[('faction_statistics', 3)]
        lines = b''.join(canonical(row) + b'\n' for row in response['data']['items'])
        temporary = stage / 'pages' / f".page-{state['pending_token']}.next"
        temporary.write_bytes(lines)

        with patch('zhangchui_mcp.export._write_package', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                download(client, self.output, schema=SCHEMA)
        resumed_state = json.loads(state_path.read_text(encoding='utf-8'))
        self.assertEqual(resumed_state['next_page'], 4)
        self.assertIsNone(resumed_state['pending_page'])
        self.assertIsNone(resumed_state['pending_token'])

        result = download(client, self.output, schema=SCHEMA)

        self.assertTrue(result['verified'])
        self.assertEqual(result['row_counts']['faction_statistics'], 3)
        row_calls = [(parameters['table'], parameters['page'])
                     for resource, parameters in client.calls if resource == 'dataset_rows']
        self.assertEqual(row_calls.count(('reports', 1)), 1)
        self.assertEqual(row_calls.count(('faction_statistics', 1)), 1)
        self.assertEqual(row_calls.count(('faction_statistics', 2)), 1)
        self.assertEqual(row_calls.count(('faction_statistics', 3)), 1)

    def test_legal_partial_page_reuses_its_token_and_leaves_no_stale_temp(self):
        manifest, pages = multipage_fixture()
        client = QuotaOnceClient(manifest, pages, fail_at=('faction_statistics', 2))
        with self.assertRaises(QueryError):
            download(client, self.output, schema=SCHEMA)
        stage = _progress_directory(self.output)
        state_path = stage / 'progress.json'
        state = json.loads(state_path.read_text(encoding='utf-8'))
        original_token = state['pending_token']
        response = client.pages[('faction_statistics', 2)]
        lines = b''.join(canonical(row) + b'\n' for row in response['data']['items'])
        partial = lines[:max(1, len(lines) // 2)]
        temporary = stage / 'pages' / f'.page-{original_token}.next'
        temporary.write_bytes(partial)

        result = download(client, self.output, schema=SCHEMA)

        self.assertTrue(result['verified'])
        self.assertEqual(list(self.root.iterdir()), [self.output])
        row_calls = [(parameters['table'], parameters['page'])
                     for resource, parameters in client.calls if resource == 'dataset_rows']
        self.assertEqual(row_calls.count(('reports', 1)), 1)
        self.assertEqual(row_calls.count(('faction_statistics', 1)), 1)
        self.assertEqual(row_calls.count(('faction_statistics', 2)), 2)
        self.assertEqual(row_calls.count(('faction_statistics', 3)), 1)

    def test_progress_directory_uses_canonical_parent_for_output_aliases(self):
        canonical_output = self.output.parent.resolve() / self.output.name
        self.assertEqual(_progress_directory(self.output), _progress_directory(canonical_output))

    def test_nonmatching_partial_page_is_preserved_and_rejected(self):
        manifest, pages = multipage_fixture()
        client = QuotaOnceClient(manifest, pages, fail_at=('faction_statistics', 2))
        with self.assertRaises(QueryError):
            download(client, self.output, schema=SCHEMA)
        stage = _progress_directory(self.output)
        state_path = stage / 'progress.json'
        state = json.loads(state_path.read_text(encoding='utf-8'))
        original_token = state['pending_token']
        response = client.pages[('faction_statistics', 2)]
        lines = b''.join(canonical(row) + b'\n' for row in response['data']['items'])
        partial = bytearray(lines[:max(1, len(lines) // 2)])
        partial[0] ^= 1
        temporary = stage / 'pages' / f'.page-{original_token}.next'
        temporary.write_bytes(partial)
        calls_before = len(client.calls)

        with self.assertRaisesRegex(ValueError, '临时分片与服务器响应不一致'):
            download(client, self.output, schema=SCHEMA)

        self.assertEqual(temporary.read_bytes(), bytes(partial))
        retained_state = json.loads(state_path.read_text(encoding='utf-8'))
        self.assertEqual(retained_state['pending_token'], original_token)
        self.assertFalse(self.output.exists())
        row_calls = [(parameters['table'], parameters['page'])
                     for resource, parameters in client.calls[calls_before:] if resource == 'dataset_rows']
        self.assertEqual(row_calls, [('faction_statistics', 2)])

    def test_cross_process_download_lock_fails_fast_without_changing_progress(self):
        manifest, pages = multipage_fixture()
        client = QuotaOnceClient(manifest, pages, fail_at=('faction_statistics', 2))
        with self.assertRaises(QueryError):
            download(client, self.output, schema=SCHEMA)

        stage = _progress_directory(self.output)
        files_before = {
            path.relative_to(stage).as_posix(): path.read_bytes()
            for path in stage.rglob('*') if path.is_file()
        }
        calls_before = deepcopy(client.calls)
        ready = self.root / 'lock-ready'
        release = self.root / 'lock-release'
        source_root = Path(__file__).resolve().parents[1] / 'src'
        environment = os.environ.copy()
        existing_pythonpath = environment.get('PYTHONPATH')
        environment['PYTHONPATH'] = str(source_root) + (
            os.pathsep + existing_pythonpath if existing_pythonpath else '')
        child_script = r'''
import sys
import time
from pathlib import Path
from zhangchui_mcp.export import _stage_lock

stage, ready, release = map(Path, sys.argv[1:4])
with _stage_lock(stage):
    ready.write_text('ready', encoding='utf-8')
    deadline = time.monotonic() + 8
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not release.exists():
        raise SystemExit('release marker timed out')
'''
        no_window = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        process = None
        try:
            process = subprocess.Popen(
                [sys.executable, '-X', 'utf8', '-c', child_script,
                 str(stage), str(ready), str(release)],
                cwd=Path(__file__).resolve().parents[1], env=environment,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', creationflags=no_window)
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                time.sleep(0.02)
            self.assertTrue(ready.is_file(), '子进程未能在时限内取得下载锁')

            started = time.monotonic()
            with self.assertRaisesRegex(ValueError, '该目录的下载仍在运行'):
                download(client, self.output, schema=SCHEMA)
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual(client.calls, calls_before)

            release.write_text('release', encoding='utf-8')
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, f'锁持有子进程失败: {stdout} {stderr}')
            process = None
            files_after = {
                path.relative_to(stage).as_posix(): path.read_bytes()
                for path in stage.rglob('*') if path.is_file()
            }
            self.assertEqual(files_after, files_before)
        finally:
            release.write_text('release', encoding='utf-8')
            if process is not None:
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=5)

        result = download(client, self.output, schema=SCHEMA)
        self.assertTrue(result['verified'])
        self.assertEqual(result['row_counts']['faction_statistics'], 3)

    def test_resume_pins_the_saved_release_when_latest_moves_forward(self):
        manifest, pages = multipage_fixture()
        newer_manifest = deepcopy(manifest)
        newer_manifest['data']['release_id'] = 'e' * 64
        newer_manifest['data']['dataset_revision'] = 'f' * 64
        newer_manifest['meta']['release_id'] = 'e' * 64
        newer_manifest['meta']['dataset_revision'] = 'f' * 64
        client = RotatingQuotaClient(manifest, newer_manifest, pages,
                                     fail_at=('faction_statistics', 2))
        with self.assertRaises(QueryError):
            download(client, self.output, schema=SCHEMA)
        client.latest_manifest = deepcopy(newer_manifest)

        result = download(client, self.output, schema=SCHEMA)

        self.assertTrue(result['verified'])
        self.assertEqual(result['release_id'], RELEASE)
        self.assertEqual(result['dataset_revision'], REVISION)
        manifests = [parameters for resource, parameters in client.calls
                     if resource == 'dataset_manifest']
        self.assertEqual(manifests[0], {'schema': SCHEMA})
        self.assertEqual(manifests[1], {'schema': SCHEMA, 'release_id': RELEASE,
                                        'dataset_revision': REVISION})
        page_requests = [parameters for resource, parameters in client.calls if resource == 'dataset_rows']
        self.assertTrue(all(parameters['release_id'] == RELEASE
                            and parameters['dataset_revision'] == REVISION for parameters in page_requests))

    def test_keyboard_interrupt_keeps_successful_pages_for_resume(self):
        manifest, pages = multipage_fixture()
        client = InterruptOnceClient(manifest, pages, interrupt_at=('faction_statistics', 2))
        with self.assertRaises(KeyboardInterrupt):
            download(client, self.output, schema=SCHEMA)
        stage = _progress_directory(self.output)
        self.assertTrue((stage / 'pages' / 'reports-00001.jsonl').is_file())
        result = download(client, self.output, schema=SCHEMA)
        self.assertTrue(result['verified'])
        row_calls = [(parameters['table'], parameters['page'])
                     for resource, parameters in client.calls if resource == 'dataset_rows']
        self.assertEqual(row_calls.count(('reports', 1)), 1)
        self.assertEqual(row_calls.count(('faction_statistics', 1)), 1)
        self.assertEqual(row_calls.count(('faction_statistics', 2)), 2)

    def test_unknown_progress_directory_is_preserved_and_not_reused(self):
        stage = _progress_directory(self.output)
        stage.mkdir()
        unknown = stage / 'keep'
        unknown.write_bytes(b'original')
        client = FakeClient()
        with self.assertRaisesRegex(ValueError, '锁文件'):
            download(client, self.output, schema=SCHEMA)
        self.assertEqual(unknown.read_bytes(), b'original')
        self.assertFalse(self.output.exists())
        self.assertEqual(client.calls, [])

    def test_revision_change_mid_download_fails_and_does_not_mix_data(self):
        manifest, pages = fixture()
        pages['faction_statistics']['meta']['dataset_revision'] = 'f' * 64
        with self.assertRaises(ValueError):
            download(FakeClient(manifest, pages), self.output, schema=SCHEMA)
        self.assertFalse(self.output.exists())

    def test_manifest_body_and_envelope_revision_must_match_before_download(self):
        manifest, pages = fixture()
        manifest['data']['dataset_revision'] = 'f' * 64
        client = FakeClient(manifest, pages)
        with self.assertRaises(ValueError):
            download(client, self.output, schema=SCHEMA)
        self.assertEqual(len(client.calls), 1)
        self.assertFalse(self.output.exists())

    def test_offline_tamper_truncation_and_added_rows_fail(self):
        download(FakeClient(), self.output, schema=SCHEMA)
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
                download(client, self.output, schema=SCHEMA)
            self.assertEqual(len(client.calls), 1)

    def test_v2_validates_both_new_table_descriptors_before_requesting_any_rows(self):
        mutations = {
            'invalid_count': lambda table: table.update(row_count=True),
            'negative_size': lambda table: table.update(jsonl_bytes=-1),
            'invalid_pages': lambda table: table.update(pages={}),
            'too_many_pages': lambda table: table.update(pages=table['pages'] * 10001),
            'invalid_rows': lambda table: table['pages'][0].update(rows=201),
            'invalid_row_type': lambda table: table['pages'][0].update(rows='one'),
            'invalid_chunk_size': lambda table: table['pages'][0].update(jsonl_bytes=262145),
            'invalid_chunk_size_type': lambda table: table['pages'][0].update(jsonl_bytes='one'),
            'invalid_chunk_type': lambda table: table.update(pages=[None]),
            'invalid_chunk_hash': lambda table: table['pages'][0].update(sha256='invalid'),
            'invalid_jsonl_hash': lambda table: table['pages'][0].update(jsonl_sha256='invalid'),
            'invalid_table_hash': lambda table: table.update(jsonl_sha256='invalid'),
        }
        for name in ('detachment_statistics', 'roster_unit_statistics'):
            for mutation_name, mutate in mutations.items():
                with self.subTest(table=name, mutation=mutation_name):
                    manifest, pages = fixture(GRANULAR_SCHEMA)
                    mutate(manifest['data']['tables'][name])
                    client = FakeClient(manifest, pages)
                    with self.assertRaises(ValueError):
                        download(client, self.output, schema=GRANULAR_SCHEMA)
                    self.assertEqual(len(client.calls), 1)
                    self.assertEqual(list(self.root.iterdir()), [])

    def test_v2_download_budget_includes_new_tables_before_requesting_any_rows(self):
        manifest, pages = fixture(GRANULAR_SCHEMA)
        tables = manifest['data']['tables']
        legacy_bytes = sum(tables[name]['jsonl_bytes'] for name in
                           ('reports', 'faction_statistics', 'matchup_statistics'))
        client = FakeClient(manifest, pages)
        with patch('zhangchui_mcp.export.MAX_BYTES', legacy_bytes + 1):
            with self.assertRaisesRegex(ValueError, '下载预算'):
                download(client, self.output, schema=GRANULAR_SCHEMA)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_folder_is_preserved(self):
        self.output.mkdir()
        file = self.output / 'keep'
        file.write_bytes(b'original')
        with self.assertRaises(ValueError):
            download(FakeClient(), self.output, schema=SCHEMA)
        self.assertEqual(file.read_bytes(), b'original')

    def test_symlink_output_is_preserved_and_rejected_before_manifest_request(self):
        protected = self.root / 'protected'
        protected.mkdir()
        file = protected / 'keep'
        file.write_bytes(b'original')
        original_is_symlink = Path.is_symlink
        try:
            self.output.symlink_to(protected, target_is_directory=True)
            is_link = lambda candidate: original_is_symlink(candidate)
        except OSError:
            # This Windows account cannot create symlinks; exercise the path check directly.
            is_link = lambda candidate: candidate == self.output
        client = FakeClient()
        with patch.object(Path, 'is_symlink', autospec=True, side_effect=is_link):
            with self.assertRaises(ValueError):
                download(client, self.output, schema=SCHEMA)
        self.assertEqual(file.read_bytes(), b'original')
        self.assertEqual(client.calls, [])

    def test_windows_junction_progress_path_is_rejected_without_touching_target(self):
        if os.name != 'nt':
            self.skipTest('Windows junction test')
        shell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
        if not shell.is_file():
            self.skipTest('Windows PowerShell is unavailable')
        protected_temp = TemporaryDirectory()
        self.addCleanup(protected_temp.cleanup)
        protected = Path(protected_temp.name)
        marker = protected / 'keep'
        marker.write_bytes(b'original')
        stage = _progress_directory(self.output)
        quote = lambda path: "'" + str(path).replace("'", "''") + "'"
        command = (f"New-Item -ItemType Junction -Path {quote(stage)} -Target {quote(protected)} "
                   "-ErrorAction Stop | Out-Null")
        created = subprocess.run([str(shell), '-NoProfile', '-Command', command],
                                 capture_output=True, text=True, timeout=20, check=False)
        if created.returncode != 0:
            self.skipTest(f'PowerShell could not create a junction: {created.stderr.strip()}')
        try:
            self.assertFalse(stage.is_symlink())
            self.assertNotEqual(getattr(stage.lstat(), 'st_reparse_tag', 0), 0)
            client = FakeClient()
            with self.assertRaisesRegex(ValueError, '进度路径'):
                download(client, self.output, schema=SCHEMA)
            self.assertEqual(client.calls, [])
            self.assertEqual(marker.read_bytes(), b'original')
            self.assertTrue(stage.is_dir())
        finally:
            os.rmdir(stage)

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
        self.assertIn('同一目录重跑会按已存release和revision重查manifest并续传', result['recovery'])

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
        download(FakeClient(manifest, pages), self.output, schema=SCHEMA)
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
            download(FakeClient(manifest, pages), output, schema=SCHEMA)
            result = recalculate(output)
            self.assertEqual(result['win_rate_rows_checked'], 1)
            self.assertEqual(result['unavailable_rows'], 2)
            self.assertEqual(result['source_methodology_rows'], 2)
            saved = json.loads((output / 'faction_statistics.jsonl').read_text().splitlines()[0])
            self.assertEqual(saved['win_rate'], rate)
            self.assertEqual(saved['win_rate_denominator'], denominator)


if __name__ == '__main__':
    unittest.main()
