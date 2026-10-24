from concurrent.futures import ThreadPoolExecutor
import io
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError

from zhangchui_mcp.client import (ClientConfig, PublicQueryClient, QueryError, GRANULAR_DATASET_SCHEMA,
                                  LEGACY_DATASET_SCHEMA)


RELEASE = 'a' * 64
REPORT = 'b' * 64
REVISION = 'c' * 64
TEST_KEY = 'synthetic-client-key-only'


def document(**extra):
    return {'data': {'items': [], 'unknown': None, **extra}, 'meta': {'schema': 'public-query/1',
        'read_only': True, 'published_at': '2026-10-22T00:00:00+00:00', 'release_id': RELEASE,
        'dataset_revision': REVISION}}


class Response(io.BytesIO):
    def __init__(self, value=None, *, raw=None, headers=None):
        super().__init__(raw if raw is not None else json.dumps(value or document()).encode())
        self.headers = {'Content-Type': 'application/json', **(headers or {})}
        self.status = 200


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def error(status, *, code='RELEASE_WITHDRAWN', retry_after=None):
    headers = {} if retry_after is None else {'Retry-After': str(retry_after)}
    raw = json.dumps({'error': {'code': code, 'message': '公开查询不可用'}}).encode()
    return HTTPError('https://example.invalid/reports', status, 'test', headers, io.BytesIO(raw))


class PublicQueryClientTests(unittest.TestCase):
    def config(self, **values):
        return ClientConfig(minimum_interval_seconds=0, api_key=TEST_KEY, **values)

    def test_get_only_request_retains_null_version_and_filters(self):
        opener = Opener(Response())
        client = PublicQueryClient(self.config(), opener=opener)
        result = client.query('catalog', release_id=RELEASE, page=1, type='weekly')
        request, timeout = opener.calls[0]
        self.assertEqual(request.method, 'GET')
        self.assertIn('/api/v1/public-query/reports?', request.full_url)
        self.assertIn('release_id=' + RELEASE, request.full_url)
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + TEST_KEY)
        self.assertNotIn('Cookie', request.headers)
        self.assertIsNone(result['data']['unknown'])
        self.assertEqual(timeout, 15)

    def test_missing_key_does_not_send_an_anonymous_request(self):
        opener = Opener()
        client = PublicQueryClient(ClientConfig(), opener=opener)
        with self.assertRaises(QueryError) as result:
            client.query('catalog')
        self.assertEqual(result.exception.document['error']['code'], 'MCP_KEY_REQUIRED')
        self.assertEqual(opener.calls, [])

    def test_revoked_key_never_reuses_cached_results_and_secret_is_not_in_repr(self):
        opener = Opener(Response(headers={'ETag': 'W/"fixed"'}), error(401, code='MCP_KEY_REVOKED'))
        client = PublicQueryClient(self.config(), opener=opener)
        client.query('catalog')
        with self.assertRaises(QueryError):
            client.query('catalog')
        self.assertEqual(len(client._cache), 0)
        self.assertNotIn(TEST_KEY, repr(client.config))
        self.assertNotIn(TEST_KEY, opener.calls[0][0].full_url)

    def test_cached_results_are_revalidated_and_return_new_objects(self):
        opener = Opener(Response(headers={'ETag': 'W/"fixed"'}), error(304))
        client = PublicQueryClient(self.config(), opener=opener)
        first = client.query('catalog', release_id=RELEASE)
        first['data']['unknown'] = 'caller mutation'
        second = client.query('catalog', release_id=RELEASE)
        self.assertEqual(opener.calls[1][0].get_header('If-none-match'), 'W/"fixed"')
        self.assertIsNone(second['data']['unknown'])
        self.assertEqual(len(opener.calls), 2)

    def test_withdrawal_or_unavailable_never_falls_back_to_cache(self):
        for status in [403, 404, 409, 410, 429, 503]:
            with self.subTest(status=status):
                opener = Opener(Response(headers={'ETag': '"fixed"'}), error(status, retry_after=17))
                client = PublicQueryClient(self.config(), opener=opener)
                client.query('catalog')
                with self.assertRaises(QueryError) as raised:
                    client.query('catalog')
                self.assertEqual(raised.exception.document['error']['status'], status)
                self.assertEqual(raised.exception.document['error']['retry_after'], 17)
                self.assertFalse(client._cache)
                self.assertEqual(len(opener.calls), 2)

    def test_network_failure_does_not_return_cached_content(self):
        opener = Opener(Response(headers={'ETag': '"fixed"'}), URLError('synthetic timeout'))
        client = PublicQueryClient(self.config(), opener=opener)
        client.query('catalog')
        with self.assertRaises(QueryError) as raised:
            client.query('catalog')
        self.assertEqual(raised.exception.document['error']['code'], 'CONNECTION_FAILED')

    def test_incomplete_http_body_and_invalid_numbers_are_not_valid_data(self):
        opener = Opener(IncompleteRead(b'partial', 100))
        with self.assertRaises(QueryError) as raised:
            PublicQueryClient(self.config(), opener=opener).query('catalog')
        self.assertEqual(raised.exception.document['error']['code'], 'CONNECTION_FAILED')
        raw = json.dumps(document()).replace('null', 'NaN').encode()
        with self.assertRaises(QueryError) as raised:
            PublicQueryClient(self.config(), opener=Opener(Response(raw=raw))).query('catalog')
        self.assertEqual(raised.exception.document['error']['code'], 'UPSTREAM_PROTOCOL')

    def test_actual_http_redirect_is_not_followed(self):
        paths = []

        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                self.send_response(302)
                self.send_header('Location', '/manage/login/')
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(('127.0.0.1', 0), Redirect)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = self.config(base_url=f'http://127.0.0.1:{server.server_port}', allow_loopback_http=True)
            with self.assertRaises(QueryError) as raised:
                PublicQueryClient(config).query('catalog')
            self.assertEqual(raised.exception.document['error']['status'], 302)
            self.assertEqual(paths, ['/reports'])
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_response_limit_is_applied_before_json_decoding(self):
        opener = Opener(Response(raw=b'x' * 2049))
        config = self.config(response_limit_bytes=2048, output_limit_bytes=1024)
        with self.assertRaises(QueryError) as raised:
            PublicQueryClient(config, opener=opener).query('catalog')
        self.assertEqual(raised.exception.document['error']['code'], 'RESPONSE_TOO_LARGE')

    def test_old_api_html_unknown_schema_and_unpublished_payload_are_rejected(self):
        wrong_schema = document()
        wrong_schema['meta']['schema'] = 'legacy'
        pending = document()
        pending['meta']['published_at'] = None
        other_release = document()
        other_release['meta']['release_id'] = 'c' * 64
        values = [Response(raw=b'<html>login</html>', headers={'Content-Type': 'text/html'}),
                  Response(wrong_schema), Response(pending), Response(other_release)]
        for response in values:
            with self.assertRaises(QueryError) as raised:
                PublicQueryClient(self.config(), opener=Opener(response)).query('catalog', release_id=RELEASE)
            self.assertEqual(raised.exception.document['error']['code'], 'UPSTREAM_PROTOCOL')

    def test_non_object_response_data_is_rejected_as_protocol_error(self):
        for payload in (None, [], 'not an object'):
            with self.subTest(payload_type=type(payload).__name__):
                invalid = document(schema=GRANULAR_DATASET_SCHEMA)
                invalid['meta']['dataset_schema'] = GRANULAR_DATASET_SCHEMA
                invalid['data'] = payload
                client = PublicQueryClient(self.config(), opener=Opener(Response(invalid)))
                with self.assertRaises(QueryError) as raised:
                    client.query('dataset_manifest', schema=GRANULAR_DATASET_SCHEMA)
                self.assertEqual(raised.exception.document['error']['code'], 'UPSTREAM_PROTOCOL')
                self.assertFalse(client._cache)

    def test_paths_parameters_and_non_https_configuration_are_rejected(self):
        for url in ['http://example.com/api', 'https://user:password@example.com/api',
                    'https://example.com/api?token=secret', 'file:///etc/passwd']:
            with self.assertRaises(ValueError):
                self.config(base_url=url)
        self.config(base_url='http://127.0.0.1:8123/api', allow_loopback_http=True)
        client = PublicQueryClient(self.config(), opener=Opener())
        for parameters in [{'url': 'http://localhost'}, {'page': True}, {'page': 0},
                           {'units_page': True}, {'units_page': 0}, {'matrix': 'players'}, {'matrix': []},
                           {'date_from': '20261022'}]:
            with self.assertRaises(QueryError):
                client.query('catalog', **parameters)
        with self.assertRaises(QueryError):
            client.query('roster', report_id=REPORT, case_id='../../manage')
        with self.assertRaises(QueryError):
            client.query('roster', report_id=REPORT, case_id=123)
        with self.assertRaises(QueryError):
            client.query(['catalog'])

    def test_cache_has_a_byte_budget_and_isolated_query_keys(self):
        opener = Opener(Response(document(value='x' * 200), headers={'ETag': '"one"'}),
                        Response(document(value='y' * 200), headers={'ETag': '"two"'}))
        client = PublicQueryClient(self.config(cache_limit_bytes=600), opener=opener)
        client.query('catalog', page=1)
        client.query('catalog', page=2)
        self.assertLessEqual(sum(len(row[1]) for row in client._cache.values()), 600)
        self.assertEqual(len(client._cache), 1)

    def test_fixed_revision_is_sent_on_all_resources_and_revalidated_from_cache(self):
        resources = [('catalog', {}), ('report', {'report_id': REPORT}),
                     ('matchups', {'report_id': REPORT}), ('rosters', {'report_id': REPORT}),
                     ('roster', {'report_id': REPORT, 'case_id': 'd' * 64}),
                     ('notes', {'report_id': REPORT}), ('dataset_manifest', {}),
                     ('dataset_rows', {'table': 'reports', 'page': 1})]
        for resource, arguments in resources:
            with self.subTest(resource=resource):
                opener = Opener(Response(headers={'ETag': '"fixed"'}), error(304))
                client = PublicQueryClient(self.config(), opener=opener)
                first = client.query(resource, release_id=RELEASE, dataset_revision=REVISION, **arguments)
                second = client.query(resource, release_id=RELEASE, dataset_revision=REVISION, **arguments)
                self.assertEqual(first, second)
                self.assertIn('dataset_revision=' + REVISION, opener.calls[0][0].full_url)
                self.assertEqual(opener.calls[1][0].get_header('If-none-match'), '"fixed"')

    def test_revision_conflict_clears_cache_and_cannot_be_silently_retried(self):
        opener = Opener(Response(headers={'ETag': '"fixed"'}), error(409, code='DATASET_REVISION_CHANGED'))
        client = PublicQueryClient(self.config(), opener=opener)
        client.query('catalog', release_id=RELEASE, dataset_revision=REVISION)
        with self.assertRaises(QueryError) as result:
            client.query('catalog', release_id=RELEASE, dataset_revision=REVISION)
        self.assertEqual(result.exception.document['error']['code'], 'DATASET_REVISION_CHANGED')
        self.assertEqual(len(opener.calls), 2)
        self.assertFalse(client._cache)

    def test_wrong_or_missing_revision_in_success_response_is_rejected_when_pinned(self):
        missing = document()
        missing['meta'].pop('dataset_revision')
        wrong = document()
        wrong['meta']['dataset_revision'] = 'd' * 64
        malformed = document()
        malformed['meta']['dataset_revision'] = 5
        for value in (missing, wrong, malformed):
            with self.subTest(value=value['meta']):
                client = PublicQueryClient(self.config(), opener=Opener(Response(value)))
                with self.assertRaises(QueryError) as result:
                    client.query('catalog', dataset_revision=REVISION)
                self.assertEqual(result.exception.document['error']['code'], 'UPSTREAM_PROTOCOL')
                self.assertFalse(client._cache)
        legacy = PublicQueryClient(self.config(), opener=Opener(Response(missing)))
        self.assertIn('data', legacy.query('catalog'))

    def test_revisions_isolate_cache_entries_and_allow_normal_updates_without_client_upgrade(self):
        changed = document()
        changed['meta']['dataset_revision'] = 'd' * 64
        opener = Opener(Response(headers={'ETag': '"first"'}), Response(changed, headers={'ETag': '"second"'}))
        client = PublicQueryClient(self.config(), opener=opener)
        client.query('catalog', dataset_revision=REVISION)
        updated = client.query('catalog', dataset_revision='d' * 64)
        self.assertEqual(updated['meta']['dataset_revision'], 'd' * 64)
        self.assertIsNone(opener.calls[1][0].get_header('If-none-match'))
        self.assertEqual(len(client._cache), 2)

    def test_dataset_routes_and_table_names_are_not_arbitrary_urls(self):
        opener = Opener(Response(), Response())
        client = PublicQueryClient(self.config(), opener=opener)
        client.query('dataset_manifest')
        client.query('dataset_rows', table='faction_statistics', page=2)
        self.assertTrue(opener.calls[0][0].full_url.endswith('/datasets/manifest'))
        self.assertIn('/datasets/rows?page=2&table=faction_statistics', opener.calls[1][0].full_url)
        for resource, parameters in [('dataset_rows', {}), ('dataset_rows', {'table': '../reports'}),
                                     ('dataset_rows', {'table': ['reports']}),
                                     ('dataset_manifest', {'table': 'reports'}),
                                     ('catalog', {'dataset_revision': 'wrong'})]:
            with self.subTest(resource=resource, parameters=parameters), self.assertRaises(QueryError):
                client.query(resource, **parameters)
        self.assertEqual(len(opener.calls), 2)

    def test_report_detail_page_and_detachment_matrix_parameters_are_forwarded(self):
        opener = Opener(Response(), Response())
        client = PublicQueryClient(self.config(), opener=opener)
        client.query('report', report_id=REPORT, units_page=2, release_id=RELEASE,
                     dataset_revision=REVISION)
        client.query('matchups', report_id=REPORT, matrix='detachments', page=2,
                     release_id=RELEASE, dataset_revision=REVISION)
        self.assertIn('units_page=2', opener.calls[0][0].full_url)
        self.assertIn('dataset_revision=' + REVISION, opener.calls[0][0].full_url)
        self.assertIn('matrix=detachments', opener.calls[1][0].full_url)
        self.assertIn('page=2', opener.calls[1][0].full_url)
        self.assertIn('dataset_revision=' + REVISION, opener.calls[1][0].full_url)

    def test_v2_is_explicit_while_v1_omits_schema_for_old_server_compatibility(self):
        legacy = document(schema=LEGACY_DATASET_SCHEMA)
        granular = document(schema=GRANULAR_DATASET_SCHEMA)
        granular['meta']['dataset_schema'] = GRANULAR_DATASET_SCHEMA
        opener = Opener(Response(legacy), Response(granular))
        client = PublicQueryClient(self.config(), opener=opener)
        client.query('dataset_manifest', schema=LEGACY_DATASET_SCHEMA)
        client.query('dataset_manifest', schema=GRANULAR_DATASET_SCHEMA)
        self.assertTrue(opener.calls[0][0].full_url.endswith('/datasets/manifest'))
        self.assertIn('schema=public-query-dataset%2F2', opener.calls[1][0].full_url)

    def test_v2_request_to_an_old_or_ignoring_server_is_explicitly_unavailable(self):
        legacy = document(schema=LEGACY_DATASET_SCHEMA)
        old_server = PublicQueryClient(self.config(), opener=Opener(Response(legacy)))
        with self.assertRaises(QueryError) as raised:
            old_server.query('dataset_manifest', schema=GRANULAR_DATASET_SCHEMA)
        self.assertEqual(raised.exception.document['error']['code'], 'DATASET_SCHEMA_UNSUPPORTED')
        self.assertFalse(old_server._cache)

        rejected = error(400, code='INVALID_FILTER')
        older_server = PublicQueryClient(self.config(), opener=Opener(rejected))
        with self.assertRaises(QueryError) as raised:
            older_server.query('dataset_rows', table='detachment_statistics',
                               schema=GRANULAR_DATASET_SCHEMA, page=1)
        self.assertEqual(raised.exception.document['error']['code'], 'DATASET_SCHEMA_UNSUPPORTED')

    def test_v2_and_legacy_table_names_are_bound_to_the_selected_schema(self):
        client = PublicQueryClient(self.config(), opener=Opener())
        with self.assertRaises(QueryError):
            client.query('dataset_rows', table='detachment_statistics', schema=LEGACY_DATASET_SCHEMA)
        self.assertEqual(client._cache, {})

    def test_concurrent_tools_have_only_one_http_request_in_flight(self):
        class ConcurrentOpener:
            def __init__(self):
                self.active = self.maximum = 0
                self.lock = threading.Lock()

            def open(self, request, timeout):
                with self.lock:
                    self.active += 1
                    self.maximum = max(self.active, self.maximum)
                time.sleep(0.01)
                with self.lock:
                    self.active -= 1
                return Response()

        opener = ConcurrentOpener()
        client = PublicQueryClient(self.config(), opener=opener)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: client.query('catalog'), range(8)))
        self.assertEqual(len(results), 8)
        self.assertEqual(opener.maximum, 1)

    def test_rate_interval_is_measured_after_the_previous_request(self):
        sleeps = []
        clock = [0.0]

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        opener = Opener(Response(), Response())
        client = PublicQueryClient(ClientConfig(minimum_interval_seconds=2, api_key=TEST_KEY), opener=opener,
                                   clock=lambda: clock[0], sleep=sleep)
        client.query('catalog')
        client.query('catalog')
        self.assertEqual(sleeps, [2.0])


if __name__ == '__main__':
    unittest.main()
