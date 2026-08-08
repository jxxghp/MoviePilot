# -*- coding: utf-8 -*-
"""
本地插件来源标识(local:// URL)的纯工具函数。

仅做字符串/URL 处理,零 app 依赖,core 与 helper 均可直接引用;PluginHelper 保留同名静态方法委托至此
(对插件/agent 调用方不变)。
"""
from pathlib import Path
from typing import Optional
from urllib.parse import quote

# 本地插件来源标识前缀
LOCAL_REPO_PREFIX = "local://"


def is_local_repo_url(repo_url: Optional[str]) -> bool:
    """
    判断是否为本地插件来源标识
    """
    return bool(repo_url and repo_url.startswith(LOCAL_REPO_PREFIX))


def make_local_repo_url(pid: str, repo_path: Optional[Path] = None,
                        package_version: Optional[str] = None) -> str:
    """
    生成本地插件安装来源标识
    """
    repo_url = f"{LOCAL_REPO_PREFIX}{quote(pid, safe='')}"
    params = []
    if repo_path:
        params.append(f"path={quote(str(repo_path), safe='/:~')}")
    if package_version:
        params.append(f"version={quote(package_version, safe='')}")
    if params:
        repo_url = f"{repo_url}?{'&'.join(params)}"
    return repo_url
