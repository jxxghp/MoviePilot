import inspect
from typing import Any


class ObjectUtils:

    @staticmethod
    def is_obj(obj: Any):
        if isinstance(obj, list) or isinstance(obj, dict):
            return True
        else:
            return str(obj).startswith("{") or str(obj).startswith("[")

    @staticmethod
    def has_arguments(func):
        """
        判断函数是否有参数
        """
        signature = inspect.signature(func)
        parameters = signature.parameters
        parameter_names = list(parameters.keys())

        # 排除 self 参数
        if parameter_names and parameter_names[0] == 'self':
            parameter_names = parameter_names[1:]

        return len(parameter_names) > 0
