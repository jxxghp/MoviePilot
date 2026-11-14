"""
会话执行图的缓存及资源所有权。

构建中的模型、中间件与已发布图使用同一资源范围；发布同步移交所有权，
取消、构建失败和缓存替换只回收退休范围，未退出的消费者继续保留对应模型。
"""

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from app.agent.llm.helper import LLMHelper
from app.agent.tools.catalog import ToolCatalogSnapshot
from app.runtime.log import logger


class GraphResources:
    """
    一次构图或临时模型调用拥有的资源，不负责创建模型或业务中间件。
    """

    def __init__(self) -> None:
        """
        按对象身份记录资源，避免模型相等比较及重复释放。
        """
        self.models: dict[int, Any] = {}
        self.middlewares: dict[int, object] = {}
        self._sealed = False

    def own_model(self, model: Any) -> None:
        """
        在创建模型后的下一个异步等待前接管其客户端。

        :param model: 当前范围使用的模型；重复登记同一对象不会增加关闭次数
        """
        self.models[id(model)] = model

    def own_middlewares(self, middlewares: Iterable[object]) -> None:
        """
        接管提供关闭能力的中间件；关停期间接管时立即封住新任务入口。

        :param middlewares: 图使用的中间件，未声明 close 的无资源中间件不登记
        """
        for middleware in middlewares:
            if callable(getattr(middleware, "close", None)):
                self.middlewares[id(middleware)] = middleware
        if self._sealed:
            self.seal()

    def seal(self) -> None:
        """
        同步禁止已接管控制器提交新任务，仍由异步回收确认真实退出。
        """
        self._sealed = True
        for middleware in self.middlewares.values():
            seal = getattr(middleware, "seal", None)
            if callable(seal):
                try:
                    seal()
                except Exception as error:
                    logger.debug(f"封住子代理中间件失败: {error}")

    @property
    def empty(self) -> bool:
        """
        所有消费者及模型都已经释放时返回 True。
        """
        return not self.models and not self.middlewares


@dataclass
class CompiledAgentBundle:
    """
    会话内可复用的执行图、构造签名及其唯一资源范围。

    :param signature: 模型、工具和运行配置的构造签名
    :param agent: 已编译的执行图
    :param streaming: 是否按流式模式构造
    :param created_at: 图构造完成时间
    :param tool_catalog: 主智能体工具目录；None 表示尚未提供
    :param subagent_catalog: 子智能体工具目录；None 表示尚未提供
    :param plugin_revision: 插件目录修订号；-1 表示尚未记录
    :param mcp_config_signature: 外部 MCP 配置签名；空串表示尚未记录
    :param catalog_checked_at: 最近目录复核时间；None 表示尚未复核
    :param resources: 图的模型和中间件资源；默认建立空范围
    """

    signature: tuple[Any, ...]
    agent: Any
    streaming: bool
    created_at: datetime
    tool_catalog: Optional[ToolCatalogSnapshot] = None
    subagent_catalog: Optional[ToolCatalogSnapshot] = None
    plugin_revision: int = -1
    mcp_config_signature: str = ""
    catalog_checked_at: Optional[datetime] = None
    resources: GraphResources = field(default_factory=GraphResources)


class AgentGraphCache:
    """
    单会话图资源 owner，统一跟踪构建、发布和退休范围。

    在会话所属事件循环内使用；资源登记与发布不等待，回收串行执行。
    关闭失败或取消时保留尚未确认释放的对象，后续失效或清理可继续回收。
    """

    def __init__(self, *, models_in_use: Callable[[], bool]) -> None:
        """
        创建空图缓存，不启动任务或取得外部资源。

        :param models_in_use: 后台复盘仍使用共享模型时返回 True，暂缓退休模型释放
        """
        self._models_in_use = models_in_use
        self._current: Optional[CompiledAgentBundle] = None
        self._building: dict[int, GraphResources] = {}
        self._retired: dict[int, GraphResources] = {}
        self._collection_lock = asyncio.Lock()
        self._sealed = False

    @property
    def current(self) -> Optional[CompiledAgentBundle]:
        """
        返回当前已发布图；失效后立即为 None，即使资源仍待回收。
        """
        return self._current

    def get(self, signature: tuple[Any, ...], streaming: bool) -> Optional[Any]:
        """
        只复用构造签名和流式模式一致的当前图。
        """
        bundle = self._current
        if bundle and bundle.signature == signature and bundle.streaming == streaming:
            return bundle.agent
        return None

    @asynccontextmanager
    async def build(self) -> AsyncIterator[GraphResources]:
        """
        登记构建范围；未发布的范围在任意退出路径退休并尝试回收。

        :yields: 模型和中间件创建后必须立即登记的资源范围
        :raises RuntimeError: 会话已经开始关闭，禁止创建新范围
        """
        if self._sealed:
            raise RuntimeError("Agent 图资源正在关闭")
        resources = GraphResources()
        self._building[id(resources)] = resources
        try:
            yield resources
        finally:
            if self._building.pop(id(resources), None) is not None:
                self._retired[id(resources)] = resources
            await self.collect()

    def publish(self, bundle: CompiledAgentBundle) -> None:
        """
        同步发布完整图并移交资源，旧图在后续回收中退休。

        :param bundle: 构造成功的图；资源应来自当前构建范围
        :raises RuntimeError: 会话已封闭，调用方范围负责回收未发布资源
        """
        if self._sealed:
            raise RuntimeError("Agent 图资源正在关闭")
        if self._current is not None and self._current.resources is not bundle.resources:
            previous = self._current.resources
            self._retired[id(previous)] = previous
        self._current = bundle
        self._building.pop(id(bundle.resources), None)

    def seal(self) -> None:
        """
        封住当前、构建中及退休控制器；随后完成的构建不能发布。
        """
        self._sealed = True
        for resources in (*self._active(), *self._retired.values()):
            resources.seal()

    def _active(self) -> tuple[GraphResources, ...]:
        """
        返回仍由前台构建或已发布图使用的资源范围。
        """
        current = (self._current.resources,) if self._current is not None else ()
        return (*current, *self._building.values())

    def retire(self) -> None:
        """
        同步使当前图失效，资源保留到下次构建退出或异步清理时回收。
        """
        if self._current is not None:
            resources = self._current.resources
            self._retired[id(resources)] = resources
            self._current = None

    async def invalidate(self) -> bool:
        """
        立即使图失效并回收资源；构建、消费者或关闭操作未收敛时返回 False。
        """
        self.retire()
        await self.collect()
        return self._current is None and not self._building and not self._retired

    @staticmethod
    async def _close_middleware(middleware: object) -> bool:
        """
        适配中间件已有同步／异步关闭合同；False 或异常表示仍需保留资源。
        """
        try:
            result = getattr(middleware, "close")()
            if inspect.isawaitable(result):
                result = await result
            return result is not False
        except Exception as error:
            logger.debug(f"关闭子代理中间件失败: {error}")
            return False

    async def _close_middlewares(self, retired: tuple[GraphResources, ...]) -> None:
        """
        先关闭退休消费者；同一控制器每轮最多尝试一次，取消不丢失未关闭对象。
        """
        attempted: set[int] = set()
        for resources in retired:
            for identity, middleware in tuple(resources.middlewares.items()):
                if identity in attempted:
                    continue
                attempted.add(identity)
                active = {key for group in self._active() for key in group.middlewares}
                if identity in active or await self._close_middleware(middleware):
                    for group in self._retired.values():
                        group.middlewares.pop(identity, None)

    async def _close_models(self, retired: tuple[GraphResources, ...]) -> None:
        """
        消费者退出后释放模型；仍被复盘或其他资源范围使用的模型继续保留。
        """
        attempted: set[int] = set()
        for resources in retired:
            for identity, model in tuple(resources.models.items()):
                if identity in attempted:
                    continue
                attempted.add(identity)
                active = {key for group in self._active() for key in group.models}
                pending = {key for group in self._retired.values() if group.middlewares for key in group.models}
                if identity not in active:
                    if identity in pending or self._models_in_use():
                        continue
                    if not await LLMHelper.close_llm(model):
                        continue
                for group in self._retired.values():
                    group.models.pop(identity, None)

    async def collect(self) -> None:
        """
        串行回收退休范围；成功的单个释放立即登记，取消后可从剩余资源继续。
        """
        async with self._collection_lock:
            retired = tuple(self._retired.values())
            await self._close_middlewares(retired)
            await self._close_models(retired)
            for resources in retired:
                if resources.empty:
                    self._retired.pop(id(resources), None)
