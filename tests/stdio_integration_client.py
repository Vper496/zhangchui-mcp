"""Invoked by the main project's isolated backend integration harness."""
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import Client, StdioServerParameters
from zhangchui_mcp.client import PublicQueryClient, QueryError
from zhangchui_mcp.configuration import configuration_from_environment
from zhangchui_mcp.export import download, verify, recalculate


async def main():
    fixture_path, output_path = map(Path, sys.argv[1:])
    fixture = json.loads(fixture_path.read_bytes())
    package_source = Path(__file__).resolve().parents[1] / 'src'
    parameters = StdioServerParameters(command=sys.executable, args=['-m', 'zhangchui_mcp', '--base-url',
        fixture['base_url'], '--allow-loopback-http'], env={'ZHANGCHUI_MIN_INTERVAL_SECONDS': '0',
        'ZHANGCHUI_API_KEY_FILE': fixture['key_file'],
        'PYTHONPATH': str(package_source),
        'PYTHONIOENCODING': 'utf-8'})
    report_id = fixture['report_id']
    pinned = {'report_id': report_id, 'release_id': fixture['release_id']}
    result = {'transport': 'stdio', 'tools': [], 'successful_calls': []}
    async with Client(parameters, cache=None, read_timeout_seconds=15) as client:
        result['tools'] = sorted(tool.name for tool in (await client.list_tools()).tools)
        assert len(result['tools']) == 14
        catalog = await client.call_tool('list_reports', {'release_id': fixture['release_id']})
        assert not catalog.is_error, catalog
        assert any(row['report_id'] == report_id for row in catalog.structured_content['data']['items'])
        revision = catalog.structured_content['meta']['dataset_revision']
        pinned['dataset_revision'] = revision
        result['successful_calls'].append('list_reports')
        # Keep the existing quota and pacing checks intact; a second user checks the six new tools.
        feature_parameters = StdioServerParameters(command=parameters.command, args=parameters.args,
            env={**parameters.env, 'ZHANGCHUI_API_KEY_FILE': fixture['feature_key_file']})
        async with Client(feature_parameters, cache=None, read_timeout_seconds=15) as feature_client:
            analysis_selection = {'faction_id': 'orks', 'format': 'individual',
                                  'date_from': '2026-01-01', 'date_to': '2026-12-31',
                                  'release_id': fixture['release_id'], 'dataset_revision': revision}
            for name in ('get_faction_analysis', 'get_faction_detachments', 'get_faction_units'):
                response = await feature_client.call_tool(name, analysis_selection)
                assert not response.is_error, response
                assert response.structured_content['meta']['dataset_revision'] == revision
                result['successful_calls'].append(name)
            events = await feature_client.call_tool('list_report_events', pinned)
            assert not events.is_error, events
            assert events.structured_content['data']['count'] > 0
            result['successful_calls'].append('list_report_events')
            catalog_units = await feature_client.call_tool('list_units', {'faction': '欧克蛮人', 'q': 'Warboss'})
            assert not catalog_units.is_error, catalog_units
            assert catalog_units.structured_content['data']['items'][0]['source_id'] == '1'
            saved_unit = await feature_client.call_tool('get_unit', {'source_id': '1', 'name': 'Warboss', 'faction': '欧克蛮人'})
            assert not saved_unit.is_error, saved_unit
            assert saved_unit.structured_content['meta']['scope'] == 'saved_unit_library'
            assert 'release_id' not in saved_unit.structured_content['meta']
            result['successful_calls'].extend(['list_units', 'get_unit'])
        report_detail = None
        for tool in ['get_report', 'get_matchups', 'list_rosters', 'get_data_notes']:
            response = await client.call_tool(tool, pinned)
            assert not response.is_error, response
            assert response.structured_content['meta']['release_id'] == fixture['release_id']
            result['successful_calls'].append(tool)
            if tool == 'get_report':
                report_detail = response.structured_content['data']
            if tool == 'list_rosters':
                case_id = response.structured_content['data']['items'][0]['case_id']
        unit_page_one = report_detail['granularity']['roster_units']['pagination']
        detachment_page_one = report_detail['granularity']['detachments']['matchups_pagination']
        assert unit_page_one['page'] == 1 and unit_page_one['page_size'] == 20
        assert detachment_page_one['page'] == 1 and detachment_page_one['page_size'] == 20
        unit_page_two = await client.call_tool('get_report', {**pinned, 'units_page': 2})
        assert not unit_page_two.is_error, unit_page_two
        unit_page_two_data = unit_page_two.structured_content['data']['granularity']['roster_units']
        assert unit_page_two_data['pagination']['page'] == 2
        assert unit_page_two_data['pagination']['count'] == unit_page_one['count']
        detachment_matrix = await client.call_tool('get_matchups', {
            **pinned, 'matrix': 'detachments', 'page': 1,
        })
        assert not detachment_matrix.is_error, detachment_matrix
        assert detachment_matrix.structured_content['data']['matrix'] == 'detachments'
        assert detachment_matrix.structured_content['data']['page'] == 1
        result['successful_calls'].extend(['get_report_units_page_2', 'get_matchups_detachment_matrix'])
        result['pagination'] = {
            'units_first_page_count': unit_page_one['count'],
            'units_second_page': unit_page_two_data['pagination']['page'],
            'detachment_matrix_page': detachment_matrix.structured_content['data']['page'],
            'detachment_matrix_count': detachment_matrix.structured_content['data']['count'],
        }
        roster = await client.call_tool('get_roster', {**pinned, 'case_id': case_id})
        assert not roster.is_error
        assert 'Warboss' in roster.structured_content['data']['roster']['text']
        assert 'New Recruit' not in roster.structured_content['data']['roster']['text']
        roster_data = roster.structured_content['data']
        structure = roster_data['structure']
        assert structure['schema'] == 'roster-structure/1'
        assert structure['units']
        assert structure['reported_points'] == 100
        warboss = next(row for row in structure['units'] if row['name_raw'] == 'Warboss')
        assert warboss['points'] == 100
        assert warboss['weapons'] and warboss['weapons'][0]['name_raw'] == 'Choppa'
        assert set(structure) <= {'schema', 'status', 'format', 'faction_id', 'reported_points',
                                  'absence_reasons', 'units', 'enhancements', 'detachments', 'coverage'}
        structure_text = json.dumps(roster_data, ensure_ascii=False)
        for private_value in ('SYNTHETIC-PRIVATE-STRUCTURE-MARKER', 'entry_id', 'raw_sha256', 'source_path'):
            assert private_value not in structure_text
        result['successful_calls'].append('get_roster')
        result['roster_structure'] = {
            'schema': structure['schema'],
            'unit_count': len(structure['units']),
            'reported_points': structure['reported_points'],
            'warboss_points': warboss['points'],
            'warboss_equipment': warboss['weapons'][0]['name_raw'],
            'private_fields_removed': True,
        }
        manifest = await client.call_tool('get_dataset_manifest',
            {'schema': 'public-query-dataset/2', 'release_id': fixture['release_id'],
             'dataset_revision': revision})
        assert not manifest.is_error, manifest
        assert manifest.structured_content['meta']['dataset_revision'] == revision
        assert manifest.structured_content['data']['schema'] == 'public-query-dataset/2'
        expected_v2_tables = {'reports', 'faction_statistics', 'matchup_statistics',
                              'detachment_statistics', 'roster_unit_statistics'}
        assert set(manifest.structured_content['data']['tables']) == expected_v2_tables
        result['successful_calls'].append('get_dataset_manifest')
        v2_row_counts = {}
        new_table_rows = {}
        for table in manifest.structured_content['data']['tables']:
            rows = await client.call_tool('get_dataset_rows', {'release_id': fixture['release_id'],
                'dataset_revision': revision, 'schema': 'public-query-dataset/2',
                'table': table, 'page': 1})
            assert not rows.is_error, rows
            assert rows.structured_content['data']['table'] == table
            v2_row_counts[table] = rows.structured_content['data']['count']
            if table in {'detachment_statistics', 'roster_unit_statistics'}:
                new_table_rows[table] = rows.structured_content['data']['items']
        assert new_table_rows['detachment_statistics'], 'v2 detachment table must contain synthetic recorded detachments'
        expected_detachment_id = fixture['war_horde_detachment_id']
        report_detachments = report_detail['granularity']['detachments']['factions']
        expected_report_detachment = next(
            row for row in report_detachments if row.get('detachment_id') == expected_detachment_id)
        assert expected_report_detachment.get('label'), 'public report must expose its translated detachment label'
        expected_exported_detachment = next(row for row in new_table_rows['detachment_statistics']
            if row.get('report_id') == report_id and row.get('row_scope') == 'faction'
            and row.get('detachment_id') == expected_detachment_id)
        assert expected_exported_detachment['label'] == expected_report_detachment['label']
        assert expected_report_detachment['faction_usage'] == 0.5
        assert expected_report_detachment['faction_usage_denominator'] == 2
        for field in ('faction_usage', 'faction_usage_denominator'):
            assert expected_report_detachment[field] == expected_exported_detachment[field]
        filtered_report = await client.call_tool('get_report', {
            **pinned, 'faction_id': 'orks', 'detachment_id': expected_detachment_id,
        })
        assert not filtered_report.is_error, filtered_report
        filtered_rows = filtered_report.structured_content['data']['granularity']['detachments']['factions']
        assert len(filtered_rows) == 1
        assert filtered_rows[0]['faction_usage'] == 0.5
        assert filtered_rows[0]['faction_usage_denominator'] == 2
        result['successful_calls'].append('get_report_filtered_faction_usage')
        assert new_table_rows['roster_unit_statistics'], 'v2 roster unit table must contain parsed synthetic units'
        assert any(row.get('name_label') == 'Warboss' for row in new_table_rows['roster_unit_statistics'])
        result['successful_calls'].append('get_dataset_rows_v2')
        result['dataset_v2'] = {
            'schema': manifest.structured_content['data']['schema'],
            'table_row_counts': v2_row_counts,
            'synthetic_detachment_visible': True,
            'detachment_id': expected_detachment_id,
            'detachment_label_matches_report': expected_report_detachment['label'],
            'faction_usage': expected_report_detachment['faction_usage'],
            'faction_usage_denominator': expected_report_detachment['faction_usage_denominator'],
            'synthetic_parseable_unit_visible': True,
        }
        legacy = await client.call_tool('get_dataset_manifest', {
            'schema': 'public-query-dataset/1', 'release_id': fixture['release_id'],
            'dataset_revision': revision,
        })
        assert not legacy.is_error, legacy
        assert legacy.structured_content['data']['schema'] == 'public-query-dataset/1'
        expected_v1_tables = {'reports', 'faction_statistics', 'matchup_statistics'}
        assert set(legacy.structured_content['data']['tables']) == expected_v1_tables
        v1_row_counts = {}
        for table in sorted(expected_v1_tables):
            rows = await client.call_tool('get_dataset_rows', {
                'release_id': fixture['release_id'], 'dataset_revision': revision,
                'schema': 'public-query-dataset/1', 'table': table, 'page': 1,
            })
            assert not rows.is_error, rows
            assert rows.structured_content['data']['table'] == table
            assert rows.structured_content['meta']['dataset_schema'] == 'public-query-dataset/1'
            v1_row_counts[table] = rows.structured_content['data']['count']
            assert v1_row_counts[table] > 0, f'legacy table {table} must keep its synthetic rows'
        result['successful_calls'].extend(['get_dataset_manifest_v1', 'get_dataset_rows_v1'])
        result['dataset_v1'] = {'schema': legacy.structured_content['data']['schema'],
                                'table_row_counts': v1_row_counts}
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
        resumed_folder = output_path.parent / 'resumed-statistics'
        completed_pages = []
        resume_manifests = []

        class ResumeClient(PublicQueryClient):
            def query(self, resource, **parameters):
                response = super().query(resource, **parameters)
                if resource == 'dataset_rows':
                    completed_pages.append((parameters['table'], parameters['page']))
                elif resource == 'dataset_manifest':
                    resume_manifests.append(parameters)
                return response

        quota_stops = 0
        for attempt in range(1, 6):
            resume_client = ResumeClient(configuration_from_environment(
                base_url=fixture['base_url'], allow_loopback_http=True,
                environ={'ZHANGCHUI_API_KEY_FILE': fixture['resume_key_file'],
                         'ZHANGCHUI_MIN_INTERVAL_SECONDS': '0'}))
            try:
                resumed = download(resume_client, resumed_folder)
                break
            except QueryError as error:
                details = error.document['error']
                assert details['status'] == 429 and details['code'] == 'MCP_DAILY_LIMIT'
                assert not resumed_folder.exists()
                quota_stops += 1
                Path(fixture['resume_reset_request']).write_text(str(quota_stops), encoding='utf-8')
                for _ in range(100):
                    ready = Path(fixture['resume_reset_ready'])
                    if ready.exists() and ready.read_text(encoding='utf-8') == str(quota_stops):
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise TimeoutError('Parent did not simulate the next synthetic quota day')
        else:
            raise AssertionError('Small-quota download did not complete within five windows')
        assert quota_stops > 0
        assert resumed['row_counts'] == exported['row_counts']
        assert len(completed_pages) == len(set(completed_pages)), completed_pages
        assert len(resume_manifests) == quota_stops + 1
        for parameters in resume_manifests[1:]:
            assert parameters['release_id'] == fixture['release_id']
            assert parameters['dataset_revision'] == revision
        assert verify(resumed_folder)['verified']
        result['quota_resume'] = {'real_http_429_observed': True, 'quota_stops': quota_stops,
                                  'completed_pages': len(completed_pages),
                                  'no_completed_page_downloaded_twice': True,
                                  'resume_manifest_pinned_to_original_revision': True,
                                  'row_counts': resumed['row_counts'], 'verified': True}
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
