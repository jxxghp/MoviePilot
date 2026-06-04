from abc import ABC, abstractmethod
from typing import Any, Union

from app.chain import ChainBase
from app.db.systemconfig_oper import SystemConfigOper
from app.schemas import ActionContext, ActionParams, ActionResult


class ActionChain(ChainBase):
    pass


class BaseAction(ABC):
    """
    工作流动作基类
    """

    # 动作ID
    _action_id = None
    # 完成标志
    _done_flag = False
    # 执行信息
    _message = ""
    # 缓存键值
    _cache_key = "WorkflowCache-%s"
    # 动作输入输出契约，由具体动作按需覆盖
    contract = {}

    def __init__(self, action_id: str):
        self._action_id = action_id
        self._done_flag = False
        self._message = ""
        self.systemconfigoper = SystemConfigOper()

    @classmethod
    @property
    @abstractmethod
    def name(cls) -> str:  # noqa
        pass

    @classmethod
    @property
    @abstractmethod
    def description(cls) -> str:  # noqa
        pass

    @classmethod
    @property
    @abstractmethod
    def data(cls) -> dict:  # noqa
        pass

    @classmethod
    def get_contract(cls) -> dict:
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
    def _build_contract_fields(cls, fields: list) -> list:
        """
        标准化动作契约字段。
        """
        result = []
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
    def message(self) -> str:
        """
        执行信息
        """
        return self._message

    def job_done(self, message: str = None):
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

    def save_cache(self, workflow_id: int, data: Union[list, str]):
        """
        保存缓存
        """
        workflow_key = self._cache_key % workflow_id
        workflow_cache = self.systemconfigoper.get(workflow_key) or {}
        action_cache = workflow_cache.get(self._action_id) or []
        if isinstance(data, list):
            for item in data:
                if item not in action_cache:
                    action_cache.append(item)
        else:
            if data not in action_cache:
                action_cache.append(data)
        workflow_cache[self._action_id] = action_cache
        self.systemconfigoper.set(workflow_key, workflow_cache)

    @abstractmethod
    def execute(self, workflow_id: int, params: ActionParams, context: ActionContext) -> ActionContext:
        """
        执行动作
        """
        raise NotImplementedError

    def execute_with_inputs(self, workflow_id: int, params: ActionParams, inputs: dict,
                            runtime: dict, context: ActionContext) -> ActionResult:
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

    def _apply_inputs_to_context(self, inputs: dict, context: ActionContext) -> None:
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
    def _apply_runtime_to_context(runtime: dict, context: ActionContext) -> None:
        """
        将运行期信息写入 runtime_state，供动作和执行状态读取。
        """
        if not runtime:
            return
        context.runtime_state = context.runtime_state or {}
        context.runtime_state["current_action_runtime"] = {
            key: value for key, value in runtime.items()
            if key != "cancel_token"
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
