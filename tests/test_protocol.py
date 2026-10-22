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
                                         'get_roster', 'get_data_notes'})
                for tool in tools:
                    self.assertTrue(tool.annotations.read_only_hint)
                    self.assertFalse(tool.annotations.destructive_hint)
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


if __name__ == '__main__':
    unittest.main()
