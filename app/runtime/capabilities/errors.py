class CapabilityError(RuntimeError):
    """Capability Runtime 错误基类。"""


class CapabilityManifestError(CapabilityError, ValueError):
    """能力声明不完整、冲突或违反当前 schema。"""


class UnknownCapabilityError(CapabilityError, KeyError):
    """请求了 Registry 中不存在的能力。"""


class CapabilityAdapterModeError(CapabilityError, TypeError):
    """调用入口与适配器并发模型不匹配。"""


class CapabilityAdapterContractError(CapabilityError, TypeError):
    """适配器回调返回值不符合已声明的并发模型。"""


class CapabilityOperationError(CapabilityError):
    """能力物化或资源生命周期转换失败。"""

    def __init__(self, capability_id: str, operation: str, error: BaseException):
        self.capability_id = capability_id
        self.operation = operation
        self.error = error
        super().__init__(f"能力 {capability_id} 执行 {operation} 失败：{error}")


class CapabilityRuntimeClosedError(CapabilityError):
    """Runtime 已进入关闭态，禁止启动或重新物化能力。"""
