"""GitHub 授权 API 的应用服务依赖。"""

from fastapi import Depends, HTTPException

from app.api.context import get_host_runtime
from app.application.github_auth import GithubAuthService
from app.startup.composition.context import HostRuntime


def get_github_auth_service(
    runtime: HostRuntime = Depends(get_host_runtime),
) -> GithubAuthService:
    """从宿主运行时取得已装配的 GitHub 授权应用服务。"""
    if runtime.system.github_auth is None:
        raise HTTPException(status_code=503, detail="GitHub 授权服务尚未初始化")
    return runtime.system.github_auth
