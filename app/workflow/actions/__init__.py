"""
定义工作流动作的公共接口和编辑器字段契约。
"""

from abc import ABC, abstractmethod
from typing import Any, ClassVar, TypedDict, TypeVar, Union

from app.application.configuration import get_configured_system_config
from app.chain.base import ChainBase
from app.domain.context import MediaInfo as DomainMediaInfo
from app.domain.context import MusicInfo as DomainMusicInfo
from app.domain.context import TorrentInfo as DomainTorrentInfo
from app.schemas.types import MediaType
from app.schemas.workflow import ActionContext, ActionResult


class _ActionFieldInput(TypedDict, total=False):
    """
    声明插件和内置动作允许提供的字段元数据，不转换已有原始值。

    :ivar name: 运行时字段的稳定名称
    :ivar label: 编辑器显示的字段名称
    :ivar kind: 编辑器使用的字段类型标识
    :ivar merge: 输入合并方式的原始声明
    :ivar identity: 区分同名字段的原始身份声明
    """

    name: str
    label: object
    kind: object
    merge: object
    identity: object


class _ActionField(TypedDict):
    """
    记录补齐展示默认值后的动作字段。

    :ivar name: 运行时字段的稳定名称
    :ivar label: 编辑器最终显示的字段名称
    :ivar kind: 编辑器最终使用的字段类型
    :ivar merge: 解析后的输入合并方式
    :ivar identity: 解析后的字段身份
    """

    name: str
    label: object
    kind: object
    merge: object
    identity: object


class _DeclaredActionContract(TypedDict, total=False):
    """
    描述动作类可选声明的输入、输出和互斥键。

    :ivar inputs: 动作接受的输入字段声明；未提供时由基类推导
    :ivar outputs: 动作产生的输出字段声明；未提供时由基类推导
    :ivar concurrency_key: 可选的并发互斥键声明
    """

    inputs: list[str | _ActionFieldInput]
    outputs: list[str | _ActionFieldInput]
    concurrency_key: object


class _ResolvedActionContract(TypedDict):
    """
    提供编辑器与运行器读取的完整动作契约投影。

    :ivar inputs: 解析后的输入字段列表
    :ivar outputs: 解析后的输出字段列表
    :ivar condition_fields: 可供后续动作条件引用的字段列表
    :ivar concurrency_key: 解析后的并发互斥键
    """

    inputs: list[_ActionField]
    outputs: list[_ActionField]
    condition_fields: list[_ActionField]
    concurrency_key: object


_CacheItem = TypeVar("_CacheItem")


class ActionChain(ChainBase):
    """
    复用已注册模块能力执行工作流动作所需的业务调用。
    """

    pass


def domain_media_info_from_dict(data: dict[str, Any]) -> DomainMediaInfo | DomainMusicInfo:
    """
    把工作流 Schema 媒体字典按媒体类型还原为下载、过滤等业务链使用的领域对象。
    """
    if data.get("type") == MediaType.MUSIC.value:
        return DomainMusicInfo.from_dict(data)
    media_info = DomainMediaInfo()
    media_info.from_dict(data)
    return media_info


def domain_torrent_info_from_dict(data: dict[str, Any]) -> DomainTorrentInfo:
    """
    把工作流 Schema 种子字典还原为提供发布时间等运行时接口的领域对象。
    """
    torrent_info = DomainTorrentInfo()
    torrent_info.from_dict(data)
    return torrent_info


class BaseAction(ABC):
    """
    工作流动作基类
    """

    # 工作流编辑器和运行管理器读取的动作级静态元数据。
    name: ClassVar[str]
    description: ClassVar[str]
    data: ClassVar[dict[str, Any]]

    # 动作ID
    _action_id = None
    # 完成标志
    _done_flag = False
    # 执行信息
    _message: str | None = ""
    # 缓存键值
    _cache_key = "WorkflowCache-%s"
    # 动作输入输出契约，由具体动作按需覆盖
    contract: ClassVar[_DeclaredActionContract] = {}

    def __init__(self, action_id: str) -> None:
        """
        建立当前动作的执行状态并绑定配置服务。
        """
        self._action_id = action_id
        self._done_flag = False
        self._message = ""
        self.systemconfigoper = get_configured_system_config()

    @classmethod
    def get_contract(cls) -> _ResolvedActionContract:
        """
        获取动作输入输出契约。
        """
        contract = getattr(cls, "contract", None) or {}
        input_fields = cls._build_contract_fields(contract.get("inputs") or [])
        output_fields = cls._build_contract_fields(contract.get("outputs") or [])
        return {
            "inputs": input_fields,
            "outputs": output_fields,
            "condition_fields": output_fields,
            "concurrency_key": contract.get("concurrency_key"),
        }

    @classmethod
    def _build_contract_fields(cls, fields: list[str | _ActionFieldInput]) -> list[_ActionField]:
        """
        标准化动作契约字段。
        """
        result: list[_ActionField] = []
        for field in fields:
            if isinstance(field, str):
                field = {"name": field}
            if not isinstance(field, dict) or not field.get("name"):
                continue
            result.append({
                "name": field["name"],
                "label": field.get("label") or field["name"],
                "kind": field.get("kind") or "scalar",
                "merge": field.get("merge"),
                "identity": field.get("identity"),
            })
        return result

    @property
    def done(self) -> bool:
        """
        判断动作是否完成
        """
        return self._done_flag

    @property
    @abstractmethod
    def success(self) -> bool:
        """
        判断动作是否成功
        """
        pass

    @property
    def message(self) -> str | None:
        """
        执行信息
        """
        return self._message

    def job_done(self, message: str | None = None) -> None:
        """
        标记动作完成
        """
        self._message = message
        self._done_flag = True

    def check_cache(self, workflow_id: int, key: str) -> bool:
        """
        检查是否处理过
        """
        workflow_key = self._cache_key % workflow_id
        workflow_cache = self.systemconfigoper.get(workflow_key) or {}
        action_cache = workflow_cache.get(self._action_id) or []
        return key in action_cache

    def save_cache(self, workflow_id: int, data: Union[list[_CacheItem], str]) -> None:
        """
        保存缓存
        """
        workflow_key = self._cache_key % workflow_id

        def merge(current: Any) -> tuple[None, Any]:
            """
            在持久化原子更新范围内保留其他动作及本动作已有的去重记录。
            """
            workflow_cache = current or {}
            action_cache = workflow_cache.get(self._action_id) or []
            items: list[_CacheItem | str] = [*data] if isinstance(data, list) else [data]
            for item in items:
                if item not in action_cache:
                    action_cache.append(item)
            workflow_cache[self._action_id] = action_cache
            return None, workflow_cache

        self.systemconfigoper.update_atomically(workflow_key, merge)

    @abstractmethod
    def execute(self, workflow_id: int, params: dict[str, object], context: ActionContext) -> ActionContext:
        """
        执行动作
        """
        raise NotImplementedError

    def execute_with_inputs(self, workflow_id: int, params: dict[str, object], inputs: dict[str, object],
                            runtime: dict[str, object], context: ActionContext) -> ActionResult:
        """
        使用显式输入与运行期信息执行动作。
        """
        self._apply_inputs_to_context(inputs=inputs, context=context)
        self._apply_runtime_to_context(runtime=runtime, context=context)
        result_context = self.execute(workflow_id, params, context)
        outputs = self._extract_outputs_from_context(result_context)
        return ActionResult(
            success=self.success,
            message=self.message,
            context=result_context,
            outputs=outputs
        )

    def _apply_inputs_to_context(self, inputs: dict[str, object], context: ActionContext) -> None:
        """
        将显式输入回填到旧版上下文字段，兼容仍读取 context 的动作。
        """
        inputs = inputs or {}
        for field in self.get_contract().get("inputs") or []:
            missing = object()
            field_name = field["name"]
            value = inputs.get(field_name, missing)
            if value is missing:
                # 兼容旧版节点输入路径，例如 outputs.A.torrents。
                for input_key, input_value in inputs.items():
                    if isinstance(input_key, str) and input_key.split(".")[-1] == field_name:
                        value = input_value
                        break
            if value is not missing:
                setattr(context, field_name, value)

    @staticmethod
    def _apply_runtime_to_context(runtime: dict[str, object], context: ActionContext) -> None:
        """
        将运行期信息写入 runtime_state，供动作和执行状态读取。
        """
        if not runtime:
            return
        context.runtime_state = context.runtime_state or {}
        context.runtime_state["current_action_runtime"] = {
            key: value
            for key, value in runtime.items()
            if key != "cancel_token"
            and isinstance(value, (dict, list, str, int, float, type(None)))
        }

    def _extract_outputs_from_context(self, context: ActionContext) -> dict[str, Any]:
        """
        按动作契约从上下文提取输出。
        """
        outputs = {}
        for field in self.get_contract().get("outputs") or []:
            value = getattr(context, field["name"], None)
            if value not in (None, "", [], {}):
                outputs[field["name"]] = value
        return outputs
