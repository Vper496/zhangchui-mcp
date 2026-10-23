"""Invoked by the main project's isolated backend integration harness."""
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import Client, StdioServerParameters
from zhangchui_mcp.client import PublicQueryClient
from zhangchui_mcp.configuration import configuration_from_environment
from zhangchui_mcp.export import download, verify, recalculate


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
        assert len(result['tools']) == 8
        catalog = await client.call_tool('list_reports', {'release_id': fixture['release_id']})
        assert not catalog.is_error, catalog
        assert any(row['report_id'] == report_id for row in catalog.structured_content['data']['items'])
        revision = catalog.structured_content['meta']['dataset_revision']
        pinned['dataset_revision'] = revision
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
        manifest = await client.call_tool('get_dataset_manifest',
                                         {'release_id': fixture['release_id'], 'dataset_revision': revision})
        assert not manifest.is_error, manifest
        assert manifest.structured_content['meta']['dataset_revision'] == revision
        result['successful_calls'].append('get_dataset_manifest')
        for table in manifest.structured_content['data']['tables']:
            rows = await client.call_tool('get_dataset_rows', {'release_id': fixture['release_id'],
                'dataset_revision': revision, 'table': table, 'page': 1})
            assert not rows.is_error, rows
            assert rows.structured_content['data']['table'] == table
        result['successful_calls'].append('get_dataset_rows')
        stale = await client.call_tool('get_report', {**pinned, 'dataset_revision': 'f' * 64})
        assert stale.is_error and stale.structured_content['error']['status'] == 409
        assert stale.structured_content['error']['code'] == 'DATASET_REVISION_CHANGED'
        export_client = PublicQueryClient(configuration_from_environment(
            base_url=fixture['base_url'], allow_loopback_http=True,
            environ={'ZHANGCHUI_API_KEY_FILE': fixture['key_file'], 'ZHANGCHUI_MIN_INTERVAL_SECONDS': '0'}))
        export_folder = output_path.parent / 'downloaded-statistics'
        exported = download(export_client, export_folder)
        assert exported['dataset_revision'] == revision
        assert verify(export_folder)['verified']
        result['offline_recalculation'] = recalculate(export_folder)
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
                      dataset_revision_pinned=True, revision_conflict_rejected=True,
                      structured_errors=True, issued_key_required=True, administrator_credentials_unused=True)
    output_path.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    asyncio.run(main())
