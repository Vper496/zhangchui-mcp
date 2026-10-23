import unittest

import anyio
from mcp import Client

from zhangchui_mcp.client import QueryError
from zhangchui_mcp.server import create_server


class FakePublicClient:
    def __init__(self):
        self.calls = []

    def query(self, resource, **parameters):
        self.calls.append((resource, parameters))
        if resource == 'notes':
            raise QueryError('QUERY_NOT_PREPARED', '当前公开查询结果未准备好', status=503)
        return {'data': {'resource': resource, 'unknown': None}, 'meta': {'release_id': 'a' * 64}}


class ProtocolTests(unittest.TestCase):
    def test_real_sdk_discovery_schema_structured_results_and_errors(self):
        async def check():
            api = FakePublicClient()
            async with Client(create_server(api), cache=None) as client:
                tools = (await client.list_tools()).tools
                names = {tool.name for tool in tools}
                self.assertEqual(names, {'list_reports', 'get_report', 'get_matchups', 'list_rosters',
                                         'get_roster', 'get_data_notes', 'get_dataset_manifest', 'get_dataset_rows'})
                for tool in tools:
                    self.assertTrue(tool.annotations.read_only_hint)
                    self.assertFalse(tool.annotations.destructive_hint)
                    self.assertIn('dataset_revision', tool.input_schema['properties'])
                result = await client.call_tool('list_reports', {})
                self.assertFalse(result.is_error)
                self.assertIsNone(result.structured_content['data']['unknown'])
                failed = await client.call_tool('get_data_notes', {'report_id': 'b' * 64})
                self.assertTrue(failed.is_error)
                self.assertEqual(failed.structured_content['error']['code'], 'QUERY_NOT_PREPARED')
                invalid = await client.call_tool('get_report', {'report_id': '../../manage'})
                self.assertTrue(invalid.is_error)
                self.assertEqual([row[0] for row in api.calls], ['catalog', 'notes'])
        anyio.run(check)

    def test_real_sdk_forwards_fixed_revisions_on_every_tool(self):
        async def check():
            api = FakePublicClient()
            fixed = {'release_id': 'a' * 64, 'dataset_revision': 'c' * 64}
            calls = [('list_reports', {}), ('get_report', {'report_id': 'b' * 64}),
                     ('get_matchups', {'report_id': 'b' * 64}), ('list_rosters', {'report_id': 'b' * 64}),
                     ('get_roster', {'report_id': 'b' * 64, 'case_id': 'd' * 64}),
                     ('get_data_notes', {'report_id': 'b' * 64}), ('get_dataset_manifest', {}),
                     ('get_dataset_rows', {'table': 'reports', 'page': 2})]
            async with Client(create_server(api), cache=None) as client:
                for name, parameters in calls:
                    await client.call_tool(name, {**fixed, **parameters})
                self.assertEqual(len(api.calls), len(calls))
                for resource, parameters in api.calls:
                    self.assertEqual(parameters['release_id'], fixed['release_id'])
                    self.assertEqual(parameters['dataset_revision'], fixed['dataset_revision'])
                before = len(api.calls)
                failed = await client.call_tool('get_dataset_rows', {'table': '../../manage', **fixed})
                self.assertTrue(failed.is_error)
                self.assertEqual(len(api.calls), before)
        anyio.run(check)


if __name__ == '__main__':
    unittest.main()
