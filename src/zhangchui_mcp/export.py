"""Download bounded public statistics and verify/recalculate them without a server."""
import argparse
from contextlib import contextmanager
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
import uuid

from .client import PublicQueryClient, QueryError
from .configuration import configuration_from_environment

SCHEMA = 'public-query-dataset/1'
TABLES = ('reports', 'faction_statistics', 'matchup_statistics')
GRANULAR_SCHEMA = 'public-query-dataset/2'
GRANULAR_TABLES = (*TABLES, 'detachment_statistics', 'roster_unit_statistics')
DEFAULT_SCHEMA = GRANULAR_SCHEMA
MAX_BYTES = 128 * 1024 * 1024
MAX_CHUNK_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
PROGRESS_FORMAT = 'zhangchui-export-progress/1'


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _check(condition, message):
    if not condition:
        raise ValueError(message)


def _is_link_or_reparse(path):
    if path.is_symlink():
        return True
    if os.name == 'nt':
        try:
            return getattr(path.lstat(), 'st_reparse_tag', 0) != 0
        except FileNotFoundError:
            return False
    return False


def _identity(meta):
    values = tuple(meta.get(key) for key in ('release_id', 'dataset_revision'))
    _check(all(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) for value in values),
           '数据包需要服务器提供完整发布版本和数据修订')
    return values


def _manifest(document, expected_schema=None):
    release, revision = _identity(document['meta'])
    data = document['data']
    schema = data.get('schema')
    tables = TABLES if schema == SCHEMA else GRANULAR_TABLES if schema == GRANULAR_SCHEMA else ()
    _check(schema in (SCHEMA, GRANULAR_SCHEMA) and data.get('release_id') == release
           and data.get('dataset_revision') == revision, '数据包格式、发布版本或数据修订无效')
    _check(expected_schema is None or schema == expected_schema, '服务端返回了与所选统计包不同的schema')
    _check(set(data['tables']) == set(tables), '数据包包含不支持的统计表')
    budget = 0
    for name in tables:
        table = data['tables'][name]
        _check(isinstance(table, dict), '统计表目录格式无效')
        _check(type(table.get('row_count')) is int and table['row_count'] >= 0
               and type(table.get('jsonl_bytes')) is int and table['jsonl_bytes'] >= 0,
               '数据包行数或大小无效')
        _check(isinstance(table.get('pages'), list) and len(table['pages']) <= 10000, '数据分片数量无效')
        for page in table['pages']:
            _check(isinstance(page, dict), '数据分片目录格式无效')
            _check(type(page.get('rows')) is int and 0 < page['rows'] <= 200
                   and type(page.get('jsonl_bytes')) is int and 0 < page['jsonl_bytes'] <= MAX_CHUNK_BYTES,
                   '单片大小或行数超过支持范围')
            for key in ('sha256', 'jsonl_sha256'):
                _check(isinstance(page.get(key), str) and re.fullmatch('[0-9a-f]{64}', page[key]), '分片校验值无效')
        _check(sum(page['rows'] for page in table['pages']) == table['row_count'], '分片行数与总行数不一致')
        _check(sum(page['jsonl_bytes'] for page in table['pages']) == table['jsonl_bytes'], '分片大小与总大小不一致')
        _check(isinstance(table.get('jsonl_sha256'), str)
               and re.fullmatch('[0-9a-f]{64}', table['jsonl_sha256']), '统计表校验值无效')
        budget += table['jsonl_bytes']
    _check(budget <= MAX_BYTES, '数据包超过128MiB下载预算')
    return data, release, revision


def _target_path(output):
    requested = Path(os.path.abspath(os.fspath(output)))
    _check(not requested.exists() and not _is_link_or_reparse(requested),
           '输出目录已存在或是符号链接，请使用新目录保留原有数据包')
    _check(requested.parent.is_dir(), '输出目录的上级目录不存在')
    parent = requested.parent.resolve()
    target = parent / requested.name
    _check(not target.exists() and not _is_link_or_reparse(target),
           '输出目录已存在或是符号链接，请使用新目录保留原有数据包')
    return target


def _progress_directory(target):
    identity = hashlib.sha256(os.fsencode(os.path.normcase(str(target)))).hexdigest()[:24]
    return target.parent / ('.zhangchui-export-' + identity)


def _page_filename(name, number):
    return f'{name}-{number:05d}.jsonl'


def _page_temporary_path(stage, state):
    return stage / 'pages' / ('.page-' + state['pending_token'] + '.next')


def _page_sequence(data, schema):
    names = TABLES if schema == SCHEMA else GRANULAR_TABLES
    return [(name, number, descriptor)
            for name in names
            for number, descriptor in enumerate(data['tables'][name]['pages'], 1)]


def _new_progress(schema, release, revision, manifest_hash):
    return {'format': PROGRESS_FORMAT, 'schema': schema, 'release_id': release,
            'dataset_revision': revision, 'manifest_sha256': manifest_hash,
            'next_page': 0, 'pending_page': None, 'pending_token': None, 'package_id': None}


def _read_limited_file(path, limit, label):
    _check(not _is_link_or_reparse(path) and path.is_file(), f'{label}不是本次下载的普通文件')
    _check(path.stat().st_size <= limit, f'{label}超过读取上限')
    raw = path.read_bytes()
    _check(len(raw) <= limit, f'{label}超过读取上限')
    return raw


def _read_progress(path, expected, total_pages):
    raw = _read_limited_file(path, 64 * 1024, '下载进度文件')
    state = json.loads(raw)
    _check(isinstance(state, dict) and set(state) == set(expected), '下载进度文件格式无效')
    for key in ('format', 'schema', 'release_id', 'dataset_revision', 'manifest_sha256'):
        _check(state[key] == expected[key], '下载进度文件与当前manifest不一致')
    _check(state['format'] == PROGRESS_FORMAT, '下载进度文件版本不受支持')
    _check(type(state['next_page']) is int and 0 <= state['next_page'] <= total_pages,
           '下载进度页码无效')
    pending = state['pending_page']
    _check(pending is None or (type(pending) is int and pending == state['next_page']
                               and pending < total_pages), '下载中的页码无效')
    token = state['pending_token']
    _check((pending is None and token is None)
           or (pending is not None and isinstance(token, str)
               and re.fullmatch('[0-9a-f]{32}', token)), '临时分片标识无效')
    package_id = state['package_id']
    _check(package_id is None or (isinstance(package_id, str)
                                  and re.fullmatch('[0-9a-f]{32}', package_id)),
           '本地组包标识无效')
    _check(package_id is None or state['next_page'] == total_pages, '数据页未完成却已有组包目录')
    _check(_encode(state) + b'\n' == raw, '下载进度文件编码无效')
    return state


def _save_progress(stage, state):
    progress = stage / 'progress.json'
    temporary = stage / 'progress.next'
    _check(not temporary.exists() and not _is_link_or_reparse(temporary),
           '发现未完成的进度文件更新，已保留现场')
    _check(not _is_link_or_reparse(progress) and progress.is_file(), '下载进度文件不是本次下载的普通文件')
    created = False
    try:
        with temporary.open('xb') as stream:
            created = True
            stream.write(_encode(state) + b'\n')
            stream.flush()
            os.fsync(stream.fileno())
        _check(not _is_link_or_reparse(temporary) and not _is_link_or_reparse(progress), '下载进度文件变成符号链接或junction')
        os.replace(temporary, progress)
    except BaseException:
        if created and temporary.exists() and not _is_link_or_reparse(temporary):
            temporary.unlink()
        raise


def _is_progress_transition(current, proposed):
    if proposed['next_page'] == current['next_page']:
        same_pending = (proposed['pending_page'] == current['pending_page']
                        and proposed['pending_token'] == current['pending_token'])
        starting_pending = (current['pending_page'] is None
                            and proposed['pending_page'] == current['next_page']
                            and proposed['pending_token'] is not None)
        same_complete_position = (current['pending_page'] is None
                                  and proposed['pending_page'] is None
                                  and proposed['pending_token'] is None)
        return same_pending or starting_pending or same_complete_position
    return (proposed['next_page'] == current['next_page'] + 1
            and current['pending_page'] == current['next_page']
            and proposed['pending_page'] is None
            and proposed['pending_token'] is None
            and proposed['package_id'] == current['package_id'])


def _recover_progress(stage, expected, total_pages):
    progress_path = stage / 'progress.json'
    next_path = stage / 'progress.next'
    current = _read_progress(progress_path, expected, total_pages)
    if next_path.exists() or _is_link_or_reparse(next_path):
        proposed = _read_progress(next_path, expected, total_pages)
        _check(_is_progress_transition(current, proposed),
               '发现无法安全接续的进度文件更新，已保留现场')
        _check(not _is_link_or_reparse(progress_path) and not _is_link_or_reparse(next_path),
               '下载进度文件变成符号链接或junction')
        os.replace(next_path, progress_path)
        current = proposed
    return current


def _initialize_progress(stage, target, manifest_raw, state):
    temporary = Path(tempfile.mkdtemp(prefix='.zhangchui-export-init-', dir=target.parent))
    try:
        (temporary / 'pages').mkdir()
        (temporary / 'download.lock').write_bytes(b'\0')
        (temporary / 'manifest.json').write_bytes(manifest_raw)
        (temporary / 'progress.json').write_bytes(_encode(state) + b'\n')
        try:
            temporary.rename(stage)
            temporary = None
        except OSError:
            if not stage.exists() and not _is_link_or_reparse(stage):
                raise
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)


@contextmanager
def _stage_lock(stage):
    _check(not _is_link_or_reparse(stage) and stage.is_dir(), '下载进度目录不是本次下载的普通目录')
    lock_path = stage / 'download.lock'
    _check(not _is_link_or_reparse(lock_path) and lock_path.is_file(), '下载锁文件不是本次下载的普通文件')
    stream = lock_path.open('r+b')
    locked = False
    try:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise ValueError('该目录的下载仍在运行，请稍后重试') from error
            locked = True
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise ValueError('该目录的下载仍在运行，请稍后重试') from error
            locked = True
        # A competing Windows byte-range lock can deny reads through a second handle.
        # Acquire the lock first, then validate through the handle that owns it.
        _check(os.fstat(stream.fileno()).st_size == 1, '下载锁文件内容无效，已保留现场')
        stream.seek(0)
        _check(stream.read(1) == b'\0', '下载锁文件内容无效，已保留现场')
        yield
    finally:
        if locked:
            if os.name == 'nt':
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def _validate_page_file(path, descriptor):
    raw = _read_limited_file(path, descriptor['jsonl_bytes'], '已下载分片')
    _check(len(raw) == descriptor['jsonl_bytes']
           and hashlib.sha256(raw).hexdigest() == descriptor['jsonl_sha256'], '本地已存分片校验失败')
    lines = raw.splitlines(keepends=True)
    _check(len(lines) == descriptor['rows'], '本地已存分片行数不一致')
    for line in lines:
        _check(line.endswith(b'\n'), '本地已存分片缺少换行')
        row = json.loads(line)
        _check(isinstance(row, dict) and _encode(row) + b'\n' == line,
               '本地已存分片编码无效')
    return raw


def _validate_progress_tree(stage, state, expected_manifest, sequence):
    allowed = {'download.lock', 'manifest.json', 'progress.json', 'pages'}
    if (stage / 'progress.next').exists() or _is_link_or_reparse(stage / 'progress.next'):
        allowed.add('progress.next')
    if state['package_id'] is not None:
        allowed.add('package-' + state['package_id'])
    _check(not _is_link_or_reparse(stage) and stage.is_dir(), '下载进度目录不是本次下载的普通目录')
    for entry in stage.iterdir():
        _check(entry.name in allowed, '下载进度目录包含未知文件，已保留现场')
        _check(not _is_link_or_reparse(entry), '下载进度目录内存在符号链接或junction，已保留现场')
    manifest = _read_limited_file(stage / 'manifest.json', MAX_MANIFEST_BYTES, '已存manifest')
    _check(manifest == expected_manifest, '当前manifest、发布或数据修订已变化，已保留旧进度；请改用新目录')
    pages = stage / 'pages'
    _check(pages.is_dir(), '下载分片目录缺失')
    expected_files = {}
    for index, (name, number, descriptor) in enumerate(sequence):
        expected_files[_page_filename(name, number)] = (index, descriptor)
    page_temp = _page_temporary_path(stage, state).name if state['pending_page'] is not None else None
    for entry in pages.iterdir():
        _check(entry.name == page_temp or entry.name in expected_files,
               '下载分片目录包含未知文件，已保留现场')
        _check(not _is_link_or_reparse(entry) and entry.is_file(), '下载分片不是本次下载的普通文件')
    return expected_files


def _load_local_progress(stage, schema):
    _check(not _is_link_or_reparse(stage) and stage.is_dir(), '下载进度目录不是本次下载的普通目录')
    manifest_raw = _read_limited_file(stage / 'manifest.json', MAX_MANIFEST_BYTES, '已存manifest')
    envelope = json.loads(manifest_raw)
    data, release, revision = _manifest(envelope, expected_schema=schema)
    _check(_encode(envelope) + b'\n' == manifest_raw, '已存manifest编码无效')
    manifest_hash = hashlib.sha256(manifest_raw).hexdigest()
    sequence = _page_sequence(data, schema)
    expected = _new_progress(schema, release, revision, manifest_hash)
    state = _recover_progress(stage, expected, len(sequence))
    _validate_progress_tree(stage, state, manifest_raw, sequence)
    return {'data': data, 'release': release, 'revision': revision,
            'manifest_raw': manifest_raw, 'sequence': sequence, 'state': state}


def _recover_pages(stage, state, sequence, expected_files):
    pages = stage / 'pages'
    pending = state['pending_page']
    temporary = _page_temporary_path(stage, state) if pending is not None else None
    if temporary is None:
        _check(not any(entry.name.startswith('.page-') for entry in pages.iterdir()),
               '发现没有对应进度记录的临时分片，已保留现场')
        temporary = pages / '.page-unexpected.next'
    if temporary.exists() or _is_link_or_reparse(temporary):
        _check(pending is not None, '发现没有对应进度记录的临时分片，已保留现场')
        _check(not _is_link_or_reparse(temporary) and temporary.is_file(), '临时分片不是本次下载的普通文件')
        name, number, descriptor = sequence[pending]
        final = pages / _page_filename(name, number)
        _check(not final.exists() and not _is_link_or_reparse(final), '临时分片与已存分片冲突，已保留现场')
        _check(temporary.stat().st_size <= descriptor['jsonl_bytes'],
               '临时分片超过manifest大小，已保留现场')
        try:
            _validate_page_file(temporary, descriptor)
        except ValueError:
            # Keep an interrupted write until a fresh verified response proves its bytes.
            pass
        else:
            temporary.rename(final)
            state['next_page'] += 1
            state['pending_page'] = None
            state['pending_token'] = None
            _save_progress(stage, state)
            pending = None

    found = {}
    for entry in pages.iterdir():
        if entry == temporary:
            continue
        index, descriptor = expected_files[entry.name]
        _check(index < state['next_page'] or (pending is not None and index == pending),
               '本地分片位置与下载进度不一致，已保留现场')
        _validate_page_file(entry, descriptor)
        found[index] = entry
    for index in range(state['next_page']):
        _check(index in found, '下载进度记录的分片缺失，已保留现场')
    if pending is not None:
        final_page = found.get(pending)
        if final_page is not None:
            state['next_page'] += 1
            state['pending_page'] = None
            state['pending_token'] = None
            _save_progress(stage, state)
            pending = None
    return state


def _page_lines(response, name, number, descriptor, table, total_pages, release, revision):
    _check(_identity(response['meta']) == (release, revision), '下载中数据修订变化，请重新下载')
    page = response['data']
    _check(page['table'] == name and page['page'] == number
           and page['chunk'] == descriptor and page['count'] == table['row_count']
           and page['has_next'] is (number < total_pages), '数据分片目录不一致')
    rows = page['items']
    _check(isinstance(rows, list) and len(rows) == descriptor['rows'], '数据分片行数不一致')
    raw = _encode({'items': rows})
    _check(len(raw) <= MAX_CHUNK_BYTES
           and hashlib.sha256(raw).hexdigest() == descriptor['sha256'], '数据分片校验失败')
    lines = b''.join(_encode(row) + b'\n' for row in rows)
    _check(len(lines) == descriptor['jsonl_bytes']
           and hashlib.sha256(lines).hexdigest() == descriptor['jsonl_sha256'], 'JSONL分片校验失败')
    return lines


def _validate_table_temporary(path, table, page_items):
    _check(not _is_link_or_reparse(path) and path.is_file(), '临时统计表不是本次组包的普通文件')
    size = path.stat().st_size
    _check(size <= table['jsonl_bytes'], '临时统计表超过manifest大小')
    remaining = size
    with path.open('rb') as stream:
        for page_path, descriptor in page_items:
            page = _validate_page_file(page_path, descriptor)
            compare_size = min(remaining, len(page))
            if compare_size:
                _check(stream.read(compare_size) == page[:compare_size],
                       '临时统计表内容不属于本次组包，已保留现场')
                remaining -= compare_size
            if remaining == 0:
                break
    _check(remaining == 0, '临时统计表内容长度无效')


def _write_package(stage, target, manifest_raw, data, sequence, state):
    if state['package_id'] is None:
        state['package_id'] = uuid.uuid4().hex
        _save_progress(stage, state)
    package = stage / ('package-' + state['package_id'])
    _check(not _is_link_or_reparse(package), '组包目录是符号链接或junction，已保留现场')
    if not package.exists():
        package.mkdir()
    _check(package.is_dir(), '组包路径不是目录，已保留现场')
    expected_names = {'manifest.json', *[name + '.jsonl' for name in data['tables']],
                      '.manifest.json.tmp', *['.' + name + '.jsonl.tmp' for name in data['tables']]}
    for entry in package.iterdir():
        _check(entry.name in expected_names and not _is_link_or_reparse(entry) and entry.is_file(),
               '组包目录包含未知文件，已保留现场')

    manifest_path = package / 'manifest.json'
    manifest_tmp = package / '.manifest.json.tmp'
    if manifest_path.exists():
        _check(not manifest_tmp.exists() and not _is_link_or_reparse(manifest_tmp),
               '完整manifest与临时manifest同时存在，已保留现场')
        _check(_read_limited_file(manifest_path, MAX_MANIFEST_BYTES, '组包manifest') == manifest_raw,
               '组包manifest与当前版本不一致，已保留现场')
    else:
        if manifest_tmp.exists():
            partial_manifest = _read_limited_file(manifest_tmp, MAX_MANIFEST_BYTES, '临时manifest')
            _check(manifest_raw.startswith(partial_manifest), '临时manifest内容不属于本次组包，已保留现场')
        with manifest_tmp.open('wb') as stream:
            stream.write(manifest_raw)
            stream.flush()
            os.fsync(stream.fileno())
        _check(not manifest_path.exists() and not _is_link_or_reparse(manifest_path), '组包manifest路径已被占用')
        manifest_tmp.rename(manifest_path)

    page_paths = [stage / 'pages' / _page_filename(name, number) for name, number, _ in sequence]
    table_pages = {}
    for index, (name, number, descriptor) in enumerate(sequence):
        table_pages.setdefault(name, []).append((page_paths[index], descriptor))
    for name, table in data['tables'].items():
        output_path = package / (name + '.jsonl')
        temporary = package / ('.' + name + '.jsonl.tmp')
        if output_path.exists() or _is_link_or_reparse(output_path):
            _check(not _is_link_or_reparse(output_path) and output_path.is_file(), '组包统计表不是普通文件')
            _check(not temporary.exists() and not _is_link_or_reparse(temporary),
                   '完整统计表与临时统计表同时存在，已保留现场')
            total = hashlib.sha256()
            count = size = 0
            for row, raw in _rows(output_path):
                total.update(raw)
                count += 1
                size += len(raw)
            _check(count == table['row_count'] and size == table['jsonl_bytes']
                   and total.hexdigest() == table['jsonl_sha256'], '已存组包统计表校验失败，已保留现场')
            continue
        if temporary.exists() or _is_link_or_reparse(temporary):
            _validate_table_temporary(temporary, table, table_pages.get(name, []))
        total = hashlib.sha256()
        count = size = 0
        with temporary.open('wb') as stream:
            for page_path, descriptor in table_pages.get(name, []):
                lines = _validate_page_file(page_path, descriptor)
                stream.write(lines)
                total.update(lines)
                count += descriptor['rows']
                size += len(lines)
            stream.flush()
            os.fsync(stream.fileno())
        _check(count == table['row_count'] and size == table['jsonl_bytes']
               and total.hexdigest() == table['jsonl_sha256'], '完整统计表校验失败')
        _check(not output_path.exists() and not _is_link_or_reparse(output_path), '组包统计表路径已被占用')
        temporary.rename(output_path)
    result = verify(package)
    _check(not target.exists() and not _is_link_or_reparse(target),
           '输出目录在下载期间已出现，请保留现有数据并改用新目录')
    package.rename(target)
    return result, package


def _cleanup_progress(stage, sequence):
    # Unlink only the exact files this download created. rmdir leaves any unknown file untouched.
    _check(not _is_link_or_reparse(stage) and stage.is_dir(), '清理时发现非本次下载进度目录')
    pages = stage / 'pages'
    if pages.exists() or _is_link_or_reparse(pages):
        _check(not _is_link_or_reparse(pages) and pages.is_dir(), '清理时发现非本次下载分片目录')
    for name, number, _ in sequence:
        page = pages / _page_filename(name, number)
        if page.exists() or _is_link_or_reparse(page):
            _check(not _is_link_or_reparse(page) and page.is_file(), '清理时发现非本次下载分片')
            page.unlink()
    if pages.exists():
        pages.rmdir()
    for name in ('progress.next', 'progress.json', 'manifest.json', 'download.lock'):
        path = stage / name
        if path.exists() or _is_link_or_reparse(path):
            _check(not _is_link_or_reparse(path) and path.is_file(), '清理时发现非本次下载文件')
            path.unlink()
    stage.rmdir()


def download(client, output, *, schema=DEFAULT_SCHEMA):
    """Resume verified public pages locally, then publish only a fully verified folder."""
    _check(schema in (SCHEMA, GRANULAR_SCHEMA), '统计包schema须为public-query-dataset/1或/2')
    target = _target_path(output)
    stage = _progress_directory(target)
    initial_envelope = None
    if stage.exists() or _is_link_or_reparse(stage):
        _check(not _is_link_or_reparse(stage) and stage.is_dir(), '同名下载进度路径已被占用，未修改')
    else:
        initial_envelope = client.query('dataset_manifest', schema=schema)
        data, release, revision = _manifest(initial_envelope, expected_schema=schema)
        manifest_raw = _encode(initial_envelope) + b'\n'
        _check(len(manifest_raw) <= MAX_MANIFEST_BYTES, '数据包manifest超过1MiB上限')
        manifest_hash = hashlib.sha256(manifest_raw).hexdigest()
        state = _new_progress(schema, release, revision, manifest_hash)
        _initialize_progress(stage, target, manifest_raw, state)
    _check(not _is_link_or_reparse(stage) and stage.is_dir(), '同名下载进度路径已被占用，未修改')
    with _stage_lock(stage):
        local = _load_local_progress(stage, schema)
        if (initial_envelope is not None
                and _identity(initial_envelope['meta']) == (local['release'], local['revision'])
                and _encode(initial_envelope) + b'\n' == local['manifest_raw']):
            envelope = initial_envelope
        else:
            envelope = client.query('dataset_manifest', schema=schema, release_id=local['release'],
                                    dataset_revision=local['revision'])
        data, release, revision = _manifest(envelope, expected_schema=schema)
        _check((release, revision) == (local['release'], local['revision']),
               '固定manifest版本与本地进度不一致，已保留现场；请改用新目录')
        manifest_raw = _encode(envelope) + b'\n'
        _check(manifest_raw == local['manifest_raw'],
               '当前manifest内容与本地进度不一致，已保留现场；请改用新目录')
        sequence = local['sequence']
        state = local['state']
        expected_files = {}
        for index, (name, number, descriptor) in enumerate(sequence):
            expected_files[_page_filename(name, number)] = (index, descriptor)
        state = _recover_pages(stage, state, sequence, expected_files)
        while state['next_page'] < len(sequence):
            index = state['next_page']
            name, number, descriptor = sequence[index]
            table = data['tables'][name]
            if state['pending_page'] is None:
                state['pending_page'] = index
                state['pending_token'] = uuid.uuid4().hex
                _save_progress(stage, state)
            else:
                _check(state['pending_page'] == index and state['pending_token'] is not None,
                       '下载进度中的临时分片位置无效，已保留现场')
            response = client.query('dataset_rows', table=name, page=number, schema=schema,
                                    release_id=release, dataset_revision=revision)
            lines = _page_lines(response, name, number, descriptor, table,
                                len(table['pages']), release, revision)
            pages = stage / 'pages'
            temporary = _page_temporary_path(stage, state)
            final = pages / _page_filename(name, number)
            _check(not final.exists() and not _is_link_or_reparse(final),
                   '目标分片路径已被占用，已保留现场')
            if temporary.exists() or _is_link_or_reparse(temporary):
                partial = _read_limited_file(temporary, descriptor['jsonl_bytes'], '临时分片')
                _check(lines.startswith(partial), '临时分片与服务器响应不一致，已保留现场')
                stream_mode = 'wb'
            else:
                stream_mode = 'xb'
            with temporary.open(stream_mode) as stream:
                stream.write(lines)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.rename(final)
            state['next_page'] += 1
            state['pending_page'] = None
            state['pending_token'] = None
            _save_progress(stage, state)
        result, package = _write_package(stage, target, manifest_raw, data, sequence, state)
    try:
        _cleanup_progress(stage, sequence)
    except (OSError, ValueError):
        # The verified output is already atomically published; an unfamiliar leftover is preserved.
        result = {**result, 'progress_directory_retained': True}
    return result


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
    for name in data['tables']:
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
    detachment_checked = detachment_usage_checked = unit_win_rate_checked = unit_usage_checked = 0
    unit_comparison_rate_checked = 0
    granular_unavailable = 0
    if 'detachment_statistics' in summary['row_counts']:
        for row, _ in _rows(Path(folder) / 'detachment_statistics.jsonl'):
            if row.get('report_source') != 'palm':
                source_methodology += 1
                granular_unavailable += 1
                continue
            if _check_rate(row, rate_field='win_rate', numerator_field='wins',
                           denominator_field='win_rate_denominator',
                           denominator_fields=('wins', 'losses')):
                detachment_checked += 1
            else:
                granular_unavailable += 1
            if _check_rate(row, rate_field='usage', numerator_field='entries',
                           denominator_field='usage_denominator'):
                detachment_usage_checked += 1
            else:
                granular_unavailable += 1
    if 'roster_unit_statistics' in summary['row_counts']:
        for row, _ in _rows(Path(folder) / 'roster_unit_statistics.jsonl'):
            if row.get('report_source') != 'palm':
                source_methodology += 1
                granular_unavailable += 1
                continue
            if _check_rate(row, rate_field='win_rate', numerator_field='wins',
                           denominator_field='win_rate_denominator',
                           denominator_fields=('wins', 'losses')):
                unit_win_rate_checked += 1
            else:
                granular_unavailable += 1
            if _check_rate(row, rate_field='usage', numerator_field='rosters',
                           denominator_field='usage_denominator'):
                unit_usage_checked += 1
            else:
                granular_unavailable += 1
            comparison = row.get('with_unit'), row.get('without_unit')
            if not all(isinstance(group, dict) for group in comparison):
                granular_unavailable += 1
                continue
            for group in comparison:
                if _check_rate(group, rate_field='win_rate', numerator_field='wins',
                               denominator_field='win_rate_denominator',
                               denominator_fields=('wins', 'losses')):
                    unit_comparison_rate_checked += 1
                else:
                    granular_unavailable += 1
    return {**summary, 'win_rate_rows_checked': checked, 'unavailable_rows': unavailable,
            'source_methodology_rows': source_methodology,
            'detachment_win_rate_rows_checked': detachment_checked,
            'detachment_usage_rows_checked': detachment_usage_checked,
            'roster_unit_win_rate_rows_checked': unit_win_rate_checked,
            'roster_unit_usage_rows_checked': unit_usage_checked,
            'roster_unit_comparison_rate_rows_checked': unit_comparison_rate_checked,
            'granular_unavailable_rows': granular_unavailable,
            'formula': 'wins/(wins+losses); draws excluded; zero denominator=null',
            'scope': 'palm rows only; each report_id separately; sources and formats never pooled',
            'source_methodology': 'Other sources retain their reported values; no inferred denominator or forced formula.'}


def _check_rate(row, *, rate_field, numerator_field, denominator_field=None, denominator_fields=()):
    numerator = row.get(numerator_field)
    denominator = row.get(denominator_field) if denominator_field else None
    if numerator is None:
        return False
    _check(type(numerator) is int and numerator >= 0, '原始细分统计计数无效')
    checked_denominator_fields = [row.get(key) for key in denominator_fields]
    if denominator_fields and any(value is None for value in checked_denominator_fields):
        return False
    if denominator_fields:
        _check(all(type(value) is int and value >= 0 for value in checked_denominator_fields),
               '原始细分统计计数无效')
        calculated_denominator = sum(checked_denominator_fields)
    else:
        calculated_denominator = None
    if denominator_field:
        if denominator is None:
            return False
        _check(type(denominator) is int and denominator >= 0, '原始细分统计分母无效')
        _check(denominator >= numerator, '原始细分统计分母小于计数')
        if calculated_denominator is not None:
            _check(denominator == calculated_denominator, '原始细分统计分母与胜负计数不一致')
    else:
        denominator = calculated_denominator
        if denominator is None:
            return False
    calculated = (numerator / denominator) if denominator else None
    reported = row.get(rate_field)
    _check((calculated is None and reported is None)
           or (calculated is not None and isinstance(reported, (int, float))
               and not isinstance(reported, bool)
               and math.isclose(calculated, reported, rel_tol=0, abs_tol=1e-12)),
           '离线细分复算与原报告比例不一致')
    return True


def main():
    parser = argparse.ArgumentParser(description='下载、离线校验并复算掌锤已公开统计；不提供原始逐局整库',
        epilog='下载每次至少间隔6.1秒，适配默认10次/分钟。若密钥额度更小，请提高'
               'ZHANGCHUI_MIN_INTERVAL_SECONDS；达到额度时会明确停止，不自动重试。')
    parser.add_argument('operation', choices=('download', 'verify', 'recalculate'))
    parser.add_argument('directory', help='download使用新目录；离线操作使用已下载目录')
    parser.add_argument('--schema', choices=(SCHEMA, GRANULAR_SCHEMA), default=DEFAULT_SCHEMA,
                        help='下载时选择统计包schema；默认v2细分包，旧服务可选v1读取原三张表')
    args = parser.parse_args()
    try:
        if args.operation == 'download':
            config = configuration_from_environment()
            # The standard issued key permits ten requests per minute. Batch downloads
            # must pace themselves; otherwise an ordinary full export fails after ten pages.
            config = replace(config, minimum_interval_seconds=max(6.1, config.minimum_interval_seconds))
            result = download(PublicQueryClient(config), args.directory, schema=args.schema)
        else:
            result = (verify if args.operation == 'verify' else recalculate)(args.directory)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except QueryError as error:
        result = error.document
        if result.get('error', {}).get('status') == 429:
            result = {**result, 'recovery': '本次未生成完成的数据包。请按retry_after等待；额度更小时提高'
                      'ZHANGCHUI_MIN_INTERVAL_SECONDS；同一目录重跑会按已存release和revision重查manifest并续传。'}
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        # File paths and invalid values can contain secrets; report no raw exception values.
        print('数据包未完成或校验失败；请检查目录、密钥配置和数据版本。', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
