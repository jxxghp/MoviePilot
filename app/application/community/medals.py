"""
社区勋章后台预热与定期核验；页面请求仅读取按实例凭据隔离的内存快照。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace

from app.domain.github import GithubAuthTransportError, GithubCommunityPort, GithubCommunityProfile
from app.runtime.log import logger
from app.runtime.tasks import TaskRegistry

# 后台每分钟检查身份与有效开关，名单正常六小时更新一次；失败或主动刷新至少间隔一分钟。
_CACHE_SECONDS = 21600
_REFRESH_COOLDOWN_SECONDS = 60
_QUERY_TIMEOUT_SECONDS = 45


class CommunityMedalService:
    """
    主动维护实例 GitHub 身份的勋章快照，不把核验和名单下载放进用户查询链路。

    后台任务由 lifespan 的 TaskRegistry 取消及回收；请求只检查本地凭据和本机开关，
    不等待后台刷新锁、OAuth 刷新或远端功能开关。旧结果只允许在同一凭据下继续展示。
    """

    def __init__(
        self, *, token_reader: Callable[[], Awaitable[str]], token_snapshot: Callable[[], str],
        transport: GithubCommunityPort, enabled_reader: Callable[[], Awaitable[bool]],
        local_enabled: Callable[[], bool], clock: Callable[[], float] = time.time,
    ) -> None:
        """
        注入后台使用的外部能力和查询路径使用的无网络本地快照。

        :param token_reader: 仅由后台使用的可刷新凭据读取器
        :param token_snapshot: 只读当前本地凭据，不触发 OAuth 刷新
        :param transport: GitHub 身份和公开勋章名单传输端口
        :param enabled_reader: 后台核验本机与远程合成的有效开关
        :param local_enabled: 无 I/O 的本机开关读取器，关闭后立即隐藏快照
        :param clock: 返回 Unix 秒的时钟，测试可注入
        """
        self._token_reader = token_reader
        self._token_snapshot = token_snapshot
        self._local_enabled = local_enabled
        self._enabled_reader = enabled_reader
        self._transport = transport
        self._clock = clock
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._key = ""
        self._revision = 0
        self._enabled: bool | None = None
        self._profile = GithubCommunityProfile()
        self._attempted_at = float("-inf")
        self._updating = False
        self._force_refresh = False

    def start(self, tasks: TaskRegistry) -> None:
        """
        启动唯一后台预热任务，不等待网络；重复启动不会重复创建任务。

        :param tasks: 当前 lifespan 的后台任务登记器，负责取消和有限等待
        """
        if self._task is None or self._task.done():
            self._task = tasks.create(self._run(), owner="community.medals")

    @property
    def enabled(self) -> bool:
        """
        返回当前身份已经核验的有效开关，供全局用户设置直接读取。

        :return: 本机开启且后台已确认对当前凭据开放
        """
        self._synchronize_identity()
        return self._local_enabled() and self._enabled is True

    @property
    def refreshing(self) -> bool:
        """
        返回后台是否尚在预热或刷新，供前端短暂查询结果而不启动新的外部核验。

        :return: 已启动且尚未完成的后台刷新状态
        """
        return bool(
            self._task is not None and not self._task.done()
            and (self._enabled is None or self._updating or self._force_refresh)
            and self._local_enabled()
        )

    def invalidate(self) -> None:
        """
        配置或授权变更后立即撤销旧显示，并唤醒已有后台任务重新核验。
        """
        self._revision += 1
        self._key = ""
        self._enabled = None
        self._profile = GithubCommunityProfile()
        self._attempted_at = float("-inf")
        self._wake.set()

    def _synchronize_identity(self) -> str:
        """
        仅按本地凭据摘要撤销旧身份；在途外部请求不能重新发布撤销前的结果。

        :return: 当前凭据摘要，未配置时为空串
        """
        token = self._token_snapshot()
        key = hashlib.sha256(token.encode()).hexdigest() if token else ""
        if key != self._key:
            self._revision += 1
            self._key = key
            self._enabled = None
            self._profile = GithubCommunityProfile(state="unavailable" if token else "unconfigured")
            self._attempted_at = float("-inf")
            self._wake.set()
        return key

    async def profile(self, *, refresh: bool = False) -> GithubCommunityProfile:
        """
        直接返回当前快照；主动刷新只通知后台，不等待任何外部请求或刷新锁。

        :param refresh: 请求后台尽快刷新，仍遵守一分钟冷却
        :return: 同一凭据的当前快照，以及后台刷新状态
        """
        key = self._synchronize_identity()
        if not self._local_enabled() or self._enabled is False:
            return GithubCommunityProfile(state="disabled")
        if refresh and self._clock() - self._attempted_at >= _REFRESH_COOLDOWN_SECONDS:
            self._force_refresh = True
            self._wake.set()
        profile = self._profile if self._enabled else GithubCommunityProfile(state="unavailable")
        stale = profile.stale or bool(
            key and profile.checked_at is not None and self._clock() - profile.checked_at >= _CACHE_SECONDS
        )
        return replace(profile, stale=stale, refreshing=self.refreshing)

    async def update(self, *, force: bool = False) -> None:
        """
        后台检查有效开关，并在首次、到期或失败冷却后更新名单。

        :param force: 忽略六小时名单周期，但不绕过一分钟冷却
        """
        async with self._lock:
            self._synchronize_identity()
            if not self._local_enabled():
                self._enabled = False
                return
            revision = self._revision
            self._updating = True
            try:
                async with asyncio.timeout(_QUERY_TIMEOUT_SECONDS):
                    enabled = await self._enabled_reader()
                    if not self._current(revision):
                        return
                    self._enabled = enabled
                    if not enabled or not self._key:
                        return
                    elapsed = self._clock() - self._attempted_at
                    if elapsed < _REFRESH_COOLDOWN_SECONDS or (
                        not force and elapsed < _CACHE_SECONDS and not self._profile.stale
                        and self._profile.state == "ready"
                    ):
                        return
                    self._attempted_at = self._clock()
                    token = await self._token_reader()
                    if not self._current(revision) or hashlib.sha256(token.encode()).hexdigest() != self._key:
                        return
                    user = await self._transport.get_user(token)
                    if user.user_id <= 0:
                        raise GithubAuthTransportError("GitHub 身份无效")
                    snapshot = await self._transport.get_medal_snapshot(user.user_id)
                    if not self._current(revision):
                        return
                    self._profile = GithubCommunityProfile(
                        state="ready", login=user.login, user_id=user.user_id, medals=snapshot.medals,
                        generated_at=snapshot.generated_at, checked_at=int(self._clock()),
                    )
            except (GithubAuthTransportError, TimeoutError) as error:
                if self._current(revision):
                    if isinstance(error, GithubAuthTransportError) and error.unauthorized:
                        self._profile = GithubCommunityProfile(state="unavailable")
                    else:
                        self._profile = replace(self._profile, stale=True)
            except Exception:
                if self._current(revision):
                    self._profile = replace(self._profile, stale=True)
                raise
            finally:
                self._updating = False

    def _current(self, revision: int) -> bool:
        """
        发布外部结果前重新检查本地身份与开关，禁止配置变更后回填旧账号。

        :param revision: 发起查询时的身份世代
        :return: 结果是否仍可发布到当前快照
        """
        self._synchronize_identity()
        return revision == self._revision and self._local_enabled()

    async def _run(self) -> None:
        """
        启动即预热，之后每分钟维护开关并按需更新名单；故障不会终止后续自动重试。
        """
        while True:
            force = self._force_refresh
            self._force_refresh = False
            self._wake.clear()
            try:
                await self.update(force=force)
            except Exception:
                # 只记录固定消息，不把凭据读取异常的内容写入日志。
                logger.warning("社区勋章后台更新失败，将在下一周期重试")
            try:
                async with asyncio.timeout(_REFRESH_COOLDOWN_SECONDS):
                    await self._wake.wait()
            except TimeoutError:
                continue
