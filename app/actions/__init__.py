class BaseAction:
    """
    工作流动作基类
    """
    async def execute(self, params: dict, context: dict) -> dict:
        raise NotImplementedError
