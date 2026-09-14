"""站点认证参数的宿主边界规范化。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Optional

from app.schemas.common import JsonData


def normalize_site_auth_params(
    site: Optional[str],
    params: Optional[Mapping[str, JsonData]],
    auth_sites: Mapping[str, JsonData],
) -> dict[str, JsonData]:
    """把站点字段名规范化为认证资源使用的站点前缀键名。

    认证资源的公共认证入口按 ``SITE_PARAMETER`` 读取参数，但旧版 API、
    CLI 配置和外部调用方可能提交资源清单中的原始字段名。保留未知字段，
    只将已知站点字段转换为资源契约键名。
    """
    normalized = dict(params or {})
    if not site or not params or not isinstance(auth_sites, Mapping):
        return normalized

    site_config = auth_sites.get(site)
    if not isinstance(site_config, Mapping):
        return normalized
    param_definitions = site_config.get("params")
    if not isinstance(param_definitions, Mapping):
        return normalized

    for raw_key in param_definitions:
        if not isinstance(raw_key, str):
            continue
        env_key = f"{site.upper()}_{raw_key.upper()}"
        if env_key in params:
            normalized[env_key] = params[env_key]
        elif raw_key in params:
            normalized[env_key] = params[raw_key]
        if raw_key != env_key:
            normalized.pop(raw_key, None)
    return normalized


def normalize_site_auth_config(
    auth_config: Optional[Mapping[str, JsonData]],
    auth_sites: Mapping[str, JsonData],
) -> Optional[dict[str, JsonData]]:
    """规范化持久化的站点认证配置，兼容旧字段名。"""
    if not auth_config:
        return None

    site = auth_config.get("site")
    params = auth_config.get("params")
    if not isinstance(site, str) or not isinstance(params, Mapping):
        return None

    normalized = dict(auth_config)
    normalized["params"] = normalize_site_auth_params(site, params, auth_sites)
    return normalized
