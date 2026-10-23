"""Shared local configuration; errors never echo credentials or file contents."""
import os
from pathlib import Path

from .client import ClientConfig, DEFAULT_BASE_URL


def configuration_from_environment(*, base_url=None, allow_loopback_http=False, environ=None):
    environment = os.environ if environ is None else environ
    key = environment.get('ZHANGCHUI_API_KEY')
    key_file = environment.get('ZHANGCHUI_API_KEY_FILE')
    if key and key_file:
        raise ValueError('ZHANGCHUI_API_KEY和ZHANGCHUI_API_KEY_FILE只设置一个')
    if key_file:
        try:
            with Path(key_file).open('r', encoding='utf-8-sig') as stream:
                key = stream.read(257).strip()
        except (OSError, UnicodeError):
            raise ValueError('无法读取密钥文件，请检查路径和权限') from None
    try:
        timeout = float(environment.get('ZHANGCHUI_TIMEOUT_SECONDS', '15'))
        interval = float(environment.get('ZHANGCHUI_MIN_INTERVAL_SECONDS', '2'))
    except (TypeError, ValueError):
        raise ValueError('超时和请求间隔应填写数字') from None
    return ClientConfig(base_url=base_url or environment.get('ZHANGCHUI_API_BASE', DEFAULT_BASE_URL),
                        allow_loopback_http=allow_loopback_http, api_key=key,
                        timeout_seconds=timeout, minimum_interval_seconds=interval)
