"""Download bounded public statistics and verify/recalculate them without a server."""
import argparse
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

from .client import PublicQueryClient, QueryError
from .configuration import configuration_from_environment

SCHEMA = 'public-query-dataset/1'
TABLES = ('reports', 'faction_statistics', 'matchup_statistics')
MAX_BYTES = 128 * 1024 * 1024
MAX_CHUNK_BYTES = 256 * 1024


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _check(condition, message):
    if not condition:
        raise ValueError(message)


def _identity(meta):
    values = tuple(meta.get(key) for key in ('release_id', 'dataset_revision'))
    _check(all(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) for value in values),
           '数据包需要服务器提供完整发布版本和数据修订')
    return values


def _manifest(document):
    release, revision = _identity(document['meta'])
    data = document['data']
    _check(data.get('schema') == SCHEMA and data.get('release_id') == release
           and data.get('dataset_revision') == revision, '数据包格式、发布版本或数据修订无效')
    _check(set(data['tables']) == set(TABLES), '数据包包含不支持的统计表')
    budget = 0
    for name in TABLES:
        table = data['tables'][name]
        _check(type(table['row_count']) is int and table['row_count'] >= 0
               and type(table['jsonl_bytes']) is int and table['jsonl_bytes'] >= 0,
               '数据包行数或大小无效')
        _check(isinstance(table['pages'], list) and len(table['pages']) <= 10000, '数据分片数量无效')
        _check(sum(page['rows'] for page in table['pages']) == table['row_count'], '分片行数与总行数不一致')
        _check(sum(page['jsonl_bytes'] for page in table['pages']) == table['jsonl_bytes'], '分片大小与总大小不一致')
        for page in table['pages']:
            _check(type(page['rows']) is int and 0 < page['rows'] <= 200
                   and type(page['jsonl_bytes']) is int and 0 < page['jsonl_bytes'] <= MAX_CHUNK_BYTES,
                   '单片大小或行数超过支持范围')
            for key in ('sha256', 'jsonl_sha256'):
                _check(isinstance(page[key], str) and re.fullmatch('[0-9a-f]{64}', page[key]), '分片校验值无效')
        _check(isinstance(table['jsonl_sha256'], str)
               and re.fullmatch('[0-9a-f]{64}', table['jsonl_sha256']), '统计表校验值无效')
        budget += table['jsonl_bytes']
    _check(budget <= MAX_BYTES, '数据包超过128MiB下载预算')
    return data, release, revision


def download(client, output):
    """Fetch the manifest once, pin every later request, then publish a complete folder."""
    target = Path(output).resolve()
    _check(not target.exists(), '输出目录已存在，请使用新目录保留原有数据包')
    _check(target.parent.is_dir(), '输出目录的上级目录不存在')
    envelope = client.query('dataset_manifest')
    data, release, revision = _manifest(envelope)
    temporary = Path(tempfile.mkdtemp(prefix='.zhangchui-export-', dir=target.parent))
    try:
        for name in TABLES:
            table = data['tables'][name]
            total = hashlib.sha256()
            count = size = 0
            with (temporary / (name + '.jsonl')).open('wb') as stream:
                for number, descriptor in enumerate(table['pages'], 1):
                    response = client.query('dataset_rows', table=name, page=number,
                                            release_id=release, dataset_revision=revision)
                    _check(_identity(response['meta']) == (release, revision), '下载中数据修订变化，请重新下载')
                    page = response['data']
                    _check(page['table'] == name and page['page'] == number
                           and page['chunk'] == descriptor and page['count'] == table['row_count']
                           and page['has_next'] is (number < len(table['pages'])), '数据分片目录不一致')
                    rows = page['items']
                    _check(isinstance(rows, list) and len(rows) == descriptor['rows'], '数据分片行数不一致')
                    raw = _encode({'items': rows})
                    _check(len(raw) <= MAX_CHUNK_BYTES
                           and hashlib.sha256(raw).hexdigest() == descriptor['sha256'], '数据分片校验失败')
                    lines = b''.join(_encode(row) + b'\n' for row in rows)
                    _check(len(lines) == descriptor['jsonl_bytes']
                           and hashlib.sha256(lines).hexdigest() == descriptor['jsonl_sha256'], 'JSONL分片校验失败')
                    stream.write(lines)
                    total.update(lines)
                    count += len(rows)
                    size += len(lines)
                stream.flush()
                os.fsync(stream.fileno())
            _check(count == table['row_count'] and size == table['jsonl_bytes']
                   and total.hexdigest() == table['jsonl_sha256'], '完整统计表校验失败')
        (temporary / 'manifest.json').write_bytes(_encode(envelope) + b'\n')
        result = verify(temporary)
        temporary.rename(target)
        return result
    finally:
        # Only a newly-created, exact temporary directory owned by this invocation.
        if temporary.exists():
            shutil.rmtree(temporary)


def _load_manifest(folder):
    with (folder / 'manifest.json').open('rb') as stream:
        raw = stream.read(1024 * 1024 + 1)
    _check(len(raw) <= 1024 * 1024, '数据包目录超过读取上限')
    return _manifest(json.loads(raw))


def _rows(path):
    with path.open('rb') as stream:
        while raw := stream.readline(MAX_CHUNK_BYTES + 1):
            _check(len(raw) <= MAX_CHUNK_BYTES and raw.endswith(b'\n'), 'JSONL行过大或缺少换行')
            row = json.loads(raw)
            _check(_encode(row) + b'\n' == raw, 'JSONL行不是目录约定的编码')
            yield row, raw


def verify(folder):
    """Offline integrity and schema checks; this does not prove data is still published."""
    folder = Path(folder).resolve()
    data, release, revision = _load_manifest(folder)
    counts = {}
    for name in TABLES:
        table = data['tables'][name]
        total, count, size, previous = hashlib.sha256(), 0, 0, None
        for row, raw in _rows(folder / (name + '.jsonl')):
            _check(isinstance(row, dict) and set(row) == set(table['fields']), '统计表字段与字典不一致')
            identity = tuple(row[key] for key in table['primary_key'])
            _check(previous is None or identity > previous, '统计范围重复或行顺序不一致')
            previous = identity
            total.update(raw)
            count += 1
            size += len(raw)
            _check(size <= table['jsonl_bytes'] and count <= table['row_count'], '统计表超过目录大小或行数')
        _check(count == table['row_count'] and size == table['jsonl_bytes']
               and total.hexdigest() == table['jsonl_sha256'], '离线统计表校验失败')
        counts[name] = count
    return {'release_id': release, 'dataset_revision': revision, 'verified': True, 'row_counts': counts,
            'note': '仅验证本地文件一致性；当前公开许可、撤下和新版本须联网查询。'}


def recalculate(folder):
    """Recompute each independent report row; never pool overlapping report periods."""
    summary = verify(folder)
    checked = unavailable = source_methodology = 0
    for name in ('faction_statistics', 'matchup_statistics'):
        for row, _ in _rows(Path(folder) / (name + '.jsonl')):
            if row.get('report_source') != 'palm':
                source_methodology += 1
                unavailable += 1
                continue
            wins, losses, denominator = (row.get(key) for key in ('wins', 'losses', 'win_rate_denominator'))
            if any(value is None for value in (wins, losses, denominator)):
                unavailable += 1
                continue
            _check(all(type(value) is int and value >= 0 for value in (wins, losses, denominator)), '原始胜负数无效')
            _check(denominator == wins + losses, '原始胜率分母与胜场加负场不一致')
            calculated = wins / denominator if denominator else None
            reported = row.get('win_rate')
            _check((calculated is None and reported is None)
                   or (calculated is not None and isinstance(reported, (int, float))
                       and not isinstance(reported, bool)
                       and math.isclose(calculated, reported, rel_tol=0, abs_tol=1e-12)),
                   '离线复算与原报告胜率不一致')
            checked += 1
    return {**summary, 'win_rate_rows_checked': checked, 'unavailable_rows': unavailable,
            'source_methodology_rows': source_methodology,
            'formula': 'wins/(wins+losses); draws excluded; zero denominator=null',
            'scope': 'palm rows only; each report_id separately; sources and formats never pooled',
            'source_methodology': 'Other sources retain their reported values; no inferred denominator or forced formula.'}


def main():
    parser = argparse.ArgumentParser(description='下载、离线校验并复算掌锤已公开统计；不提供原始逐局整库',
        epilog='下载每次至少间隔6.1秒，适配默认10次/分钟。若密钥额度更小，请提高'
               'ZHANGCHUI_MIN_INTERVAL_SECONDS；达到额度时会明确停止，不自动重试。')
    parser.add_argument('operation', choices=('download', 'verify', 'recalculate'))
    parser.add_argument('directory', help='download使用新目录；离线操作使用已下载目录')
    args = parser.parse_args()
    try:
        if args.operation == 'download':
            config = configuration_from_environment()
            # The standard issued key permits ten requests per minute. Batch downloads
            # must pace themselves; otherwise an ordinary full export fails after ten pages.
            config = replace(config, minimum_interval_seconds=max(6.1, config.minimum_interval_seconds))
            result = download(PublicQueryClient(config), args.directory)
        else:
            result = (verify if args.operation == 'verify' else recalculate)(args.directory)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except QueryError as error:
        result = error.document
        if result.get('error', {}).get('status') == 429:
            result = {**result, 'recovery': '本次未生成完成的数据包。请按retry_after等待；额度更小时提高'
                      'ZHANGCHUI_MIN_INTERVAL_SECONDS，再使用新目录重试。'}
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        # File paths and invalid values can contain secrets; report no raw exception values.
        print('数据包未完成或校验失败；请检查目录、密钥配置和数据版本。', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
