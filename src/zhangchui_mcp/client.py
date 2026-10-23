"""Bounded GET-only client with live conditional revalidation and no stale fallback."""
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime
from http.client import HTTPException
import ipaddress
import json
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from . import __version__

API_SCHEMA = 'public-query/1'
DEFAULT_BASE_URL = 'https://40kdata.online/api/v1/public-query'
DATASET_TABLES = frozenset({'reports', 'faction_statistics', 'matchup_statistics'})


class QueryError(Exception):
    def __init__(self, code, message, *, status=None, retry_after=None):
        super().__init__(message)
        self.document = {'error': {'code': code, 'message': message, 'status': status, 'retry_after': retry_after}}


@dataclass(frozen=True)
class ClientConfig:
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = 15.0
    minimum_interval_seconds: float = 2.0
    response_limit_bytes: int = 1024 * 1024
    output_limit_bytes: int = 512 * 1024
    cache_limit_bytes: int = 4 * 1024 * 1024
    allow_loopback_http: bool = False
    api_key: str | None = field(default=None, repr=False)

    def __post_init__(self):
        if self.api_key is not None and (not isinstance(self.api_key, str) or not 16 <= len(self.api_key) <= 256
                or not re.fullmatch(r'[A-Za-z0-9_-]+', self.api_key)):
            raise ValueError('访问密钥格式无效，请填写发放给你的完整密钥')
        try:
            parsed = urlsplit(self.base_url)
            port = parsed.port
        except ValueError as error:
            raise ValueError('API地址格式无效，访问密钥请单独配置') from None
        local = parsed.hostname == 'localhost'
        try:
            local = local or ipaddress.ip_address(parsed.hostname or '').is_loopback
        except ValueError:
            pass
        valid_scheme = parsed.scheme == 'https' or (parsed.scheme == 'http' and local and self.allow_loopback_http)
        if (not valid_scheme or not parsed.hostname or (port is not None and not 1 <= port <= 65535)
                or any(character.isspace() for character in self.base_url) or parsed.username or parsed.password
                or parsed.query or parsed.fragment or '..' in parsed.path.split('/')):
            raise ValueError('API地址必须是无凭据的HTTPS地址；开发HTTP仅允许显式启用的回环地址')
        if not 0 < self.timeout_seconds <= 60 or not 0 <= self.minimum_interval_seconds <= 60:
            raise ValueError('超时和请求间隔设置无效')
        if not 0 < self.output_limit_bytes <= self.response_limit_bytes <= 2 * 1024 * 1024:
            raise ValueError('响应大小设置无效')
        if not 0 <= self.cache_limit_bytes <= 16 * 1024 * 1024:
            raise ValueError('缓存上限须在0到16MiB之间')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def _invalid_number(value):
    raise ValueError('公开结果不能含NaN或Infinity')


def _identifier(value, name):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise QueryError('INVALID_ARGUMENT', name + '须使用查询结果中的64位标识')
    return value


def _parameters(parameters):
    allowed = {'release_id', 'dataset_revision', 'page', 'table', 'type', 'format', 'version_id', 'report_source',
               'date_from', 'date_to', 'faction_id', 'opponent_id'}
    if set(parameters) - allowed:
        raise QueryError('INVALID_ARGUMENT', '包含未支持的查询参数')
    result = {key: value for key, value in parameters.items() if value is not None}
    for key in ('release_id', 'dataset_revision'):
        if key in result:
            _identifier(result[key], key)
    if 'table' in result and (not isinstance(result['table'], str) or result['table'] not in DATASET_TABLES):
        raise QueryError('INVALID_ARGUMENT', 'table须使用manifest列出的公开统计表名')
    if 'page' in result and (type(result['page']) is not int or not 1 <= result['page'] <= 10000):
        raise QueryError('INVALID_ARGUMENT', 'page须为1到10000的整数')
    for key, value in result.items():
        if key != 'page' and (not isinstance(value, str) or not value or len(value) > 160):
            raise QueryError('INVALID_ARGUMENT', '查询参数不能为空或过长')
    for key in ['date_from', 'date_to']:
        if key in result:
            try:
                if date.fromisoformat(result[key]).isoformat() != result[key]:
                    raise ValueError()
            except ValueError as error:
                raise QueryError('INVALID_ARGUMENT', '日期须为YYYY-MM-DD') from error
    if result.get('date_from', '') > result.get('date_to', '9999-12-31'):
        raise QueryError('INVALID_ARGUMENT', '开始日期不能晚于结束日期')
    return result


class PublicQueryClient:
    def __init__(self, config=None, *, opener=None, clock=time.monotonic, sleep=time.sleep):
        self.config = config or ClientConfig()
        self.opener = opener or build_opener(ProxyHandler({}), _NoRedirect())
        self.clock, self.sleep = clock, sleep
        self._lock = threading.Lock()
        self._last_finished = None
        self._cache = OrderedDict()

    def query(self, resource, *, report_id=None, case_id=None, **parameters):
        suffixes = {'catalog': '', 'report': '', 'matchups': '/matchups',
                    'rosters': '/rosters', 'roster': '/rosters/', 'notes': '/data-notes'}
        if not isinstance(resource, str):
            raise QueryError('INVALID_ARGUMENT', '工具只支持公开查询')
        if resource in {'dataset_manifest', 'dataset_rows'}:
            path = '/datasets/' + ('manifest' if resource == 'dataset_manifest' else 'rows')
        elif resource in suffixes:
            path = '/reports'
            if resource != 'catalog':
                path += '/' + _identifier(report_id, 'report_id')
            path += suffixes[resource]
            if resource == 'roster':
                path += _identifier(case_id, 'case_id')
        else:
            raise QueryError('INVALID_ARGUMENT', '工具只支持公开查询')
        params = _parameters(parameters)
        if resource == 'dataset_rows' and 'table' not in params:
            raise QueryError('INVALID_ARGUMENT', '请先从manifest选择table，再读取统计表')
        if resource != 'dataset_rows' and 'table' in params:
            raise QueryError('INVALID_ARGUMENT', '只有统计表读取支持table参数')
        url = self.config.base_url.rstrip('/') + path
        if params:
            url += '?' + urlencode(sorted(params.items()))
        with self._lock:
            if self._last_finished is not None:
                delay = self.config.minimum_interval_seconds - (self.clock() - self._last_finished)
                if delay > 0:
                    self.sleep(delay)
            try:
                return self._request(url, params.get('release_id'), params.get('dataset_revision'))
            finally:
                self._last_finished = self.clock()

    def _request(self, url, release_id, dataset_revision):
        if not self.config.api_key:
            raise QueryError('MCP_KEY_REQUIRED', '请在MCP配置里设置ZHANGCHUI_API_KEY，填写发放给你的密钥', status=401)
        cached = self._cache.get(url)
        headers = {'Accept': 'application/json', 'Accept-Encoding': 'identity',
                   'User-Agent': 'zhangchui-mcp/' + __version__, 'Authorization': 'Bearer ' + self.config.api_key}
        if cached:
            headers['If-None-Match'] = cached[0]
        request = Request(url, headers=headers, method='GET')
        try:
            try:
                response = self.opener.open(request, timeout=self.config.timeout_seconds)
            except HTTPError as error:
                if error.code == 304 and cached:
                    error.close()
                    self._cache.move_to_end(url)
                    return self._decode(cached[1], release_id, dataset_revision)
                with error:
                    raw = error.read(min(self.config.response_limit_bytes, 65536) + 1)
                    raise self._upstream_error(error.code, error.headers, raw)
            with response:
                if response.status != 200:
                    raise QueryError('UPSTREAM_PROTOCOL', '公开查询服务返回了未支持的成功状态')
                encoding = response.headers.get('Content-Encoding', 'identity').lower()
                if encoding not in ('identity', ''):
                    raise QueryError('UPSTREAM_PROTOCOL', '服务返回了未支持的压缩格式')
                if 'application/json' not in response.headers.get('Content-Type', '').lower():
                    raise QueryError('UPSTREAM_PROTOCOL', '服务没有返回公开查询JSON')
                raw = response.read(self.config.response_limit_bytes + 1)
                if len(raw) > self.config.response_limit_bytes:
                    raise QueryError('RESPONSE_TOO_LARGE', '服务响应超过读取上限，请缩小查询范围')
                data = self._decode(raw, release_id, dataset_revision)
                etag = response.headers.get('ETag')
                if etag and len(etag) <= 256:
                    self._remember(url, etag, raw)
                return data
        except QueryError:
            self._cache.pop(url, None)
            raise
        except (OSError, URLError, TimeoutError, HTTPException) as error:
            self._cache.pop(url, None)
            raise QueryError('CONNECTION_FAILED', '无法连接公开查询服务，请稍后重试') from error

    def _decode(self, raw, release_id, dataset_revision):
        try:
            data = json.loads(raw, parse_constant=_invalid_number)
            meta = data['meta']
            if meta['schema'] != API_SCHEMA or meta['read_only'] is not True or not meta['published_at']:
                raise ValueError()
            if not isinstance(meta['release_id'], str) or not re.fullmatch('[0-9a-f]{64}', meta['release_id']):
                raise ValueError()
            datetime.fromisoformat(meta['published_at'].replace('Z', '+00:00'))
            if release_id is not None and meta['release_id'] != release_id:
                raise ValueError()
            # Unpinned calls remain compatible with older servers. A pinned call
            # must prove its revision, even if an upstream incorrectly returns 200.
            revision = meta.get('dataset_revision')
            if revision is not None and (not isinstance(revision, str) or not re.fullmatch('[0-9a-f]{64}', revision)):
                raise ValueError()
            if dataset_revision is not None and revision != dataset_revision:
                raise ValueError()
            if 'data' not in data:
                raise ValueError()
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise QueryError('UPSTREAM_PROTOCOL', '服务响应不是有效的已上线公开查询结果') from error
        output = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode()
        if len(output) > self.config.output_limit_bytes:
            raise QueryError('RESPONSE_TOO_LARGE', '查询结果超过工具输出上限，请缩小范围')
        return data

    def _remember(self, url, etag, raw):
        if len(raw) > self.config.cache_limit_bytes:
            return
        self._cache[url] = (etag, raw)
        self._cache.move_to_end(url)
        while sum(len(row[1]) for row in self._cache.values()) > self.config.cache_limit_bytes:
            self._cache.popitem(last=False)

    @staticmethod
    def _upstream_error(status, headers, raw):
        code, message = 'UPSTREAM_HTTP_ERROR', '公开查询服务暂时不可用'
        try:
            error = json.loads(raw)['error']
            if isinstance(error.get('code'), str) and isinstance(error.get('message'), str):
                code, message = error['code'][:80], error['message'][:300]
        except (ValueError, KeyError, TypeError, AttributeError):
            pass
        retry = headers.get('Retry-After', '')
        seconds = int(retry) if re.fullmatch('[0-9]{1,5}', retry) else None
        return QueryError(code, message, status=status, retry_after=seconds)
