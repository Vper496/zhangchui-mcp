"""Invoked by the main project's isolated backend integration harness."""
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import Client, StdioServerParameters


async def main():
    fixture_path, output_path = map(Path, sys.argv[1:])
    fixture = json.loads(fixture_path.read_bytes())
    parameters = StdioServerParameters(command=sys.executable, args=['-m', 'zhangchui_mcp', '--base-url',
        fixture['base_url'], '--allow-loopback-http'], env={'ZHANGCHUI_MIN_INTERVAL_SECONDS': '0',
        'ZHANGCHUI_API_KEY_FILE': fixture['key_file'],
        'PYTHONIOENCODING': 'utf-8'})
    report_id = fixture['report_id']
    pinned = {'report_id': report_id, 'release_id': fixture['release_id']}
    result = {'transport': 'stdio', 'tools': [], 'successful_calls': []}
    async with Client(parameters, cache=None, read_timeout_seconds=15) as client:
        result['tools'] = sorted(tool.name for tool in (await client.list_tools()).tools)
        assert len(result['tools']) == 6
        catalog = await client.call_tool('list_reports', {'release_id': fixture['release_id']})
        assert not catalog.is_error, catalog
        assert any(row['report_id'] == report_id for row in catalog.structured_content['data']['items'])
        result['successful_calls'].append('list_reports')
        for tool in ['get_report', 'get_matchups', 'list_rosters', 'get_data_notes']:
            response = await client.call_tool(tool, pinned)
            assert not response.is_error, response
            assert response.structured_content['meta']['release_id'] == fixture['release_id']
            result['successful_calls'].append(tool)
            if tool == 'list_rosters':
                case_id = response.structured_content['data']['items'][0]['case_id']
        roster = await client.call_tool('get_roster', {**pinned, 'case_id': case_id})
        assert not roster.is_error
        assert 'Warboss' in roster.structured_content['data']['roster']['text']
        assert 'New Recruit' not in roster.structured_content['data']['roster']['text']
        result['successful_calls'].append('get_roster')
        again = await client.call_tool('get_report', pinned)
        assert not again.is_error
        pending = await client.call_tool('list_reports', {'release_id': fixture['pending_release_id']})
        assert pending.is_error and pending.structured_content['error']['status'] == 404
        Path(fixture['withdraw_request']).write_text('withdraw', encoding='utf-8')
        for _ in range(100):
            if Path(fixture['withdraw_ready']).exists():
                break
            await asyncio.sleep(0.1)
        else:
            raise TimeoutError('Parent did not complete the synthetic withdrawal')
        withdrawn = await client.call_tool('get_report', pinned)
        assert withdrawn.is_error and withdrawn.structured_content['error']['status'] == 410
        result.update(unpublished_rejected=True, withdrawal_precedes_cached_content=True,
                      structured_errors=True, issued_key_required=True, administrator_credentials_unused=True)
    output_path.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    asyncio.run(main())
