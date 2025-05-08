import dis
import inspect
from types import FunctionType
from typing import Any, Callable, get_type_hints


class ObjectUtils:

    @staticmethod
    def is_obj(obj: Any):
        if isinstance(obj, list) \
                or isinstance(obj, dict) \
                or isinstance(obj, tuple):
            return True
        elif isinstance(obj, int) \
                or isinstance(obj, float) \
                or isinstance(obj, bool) \
                or isinstance(obj, bytes) \
                or isinstance(obj, str):
            return False
        return True

    @staticmethod
    def is_objstr(obj: Any):
        if not isinstance(obj, str):
            return False
        return str(obj).startswith("{") \
            or str(obj).startswith("[") \
            or str(obj).startswith("(")

    @staticmethod
    def arguments(func: Callable) -> int:
        """
        返回函数的参数个数
        """
        signature = inspect.signature(func)
        parameters = signature.parameters

        return len(list(parameters.keys()))

    @staticmethod
    def check_method(func: FunctionType) -> bool:
        """
        检查函数是否已实现
        """
        try:
            # 尝试通过源代码分析
            source = inspect.getsource(func)
            in_comment = False
            for line in source.split('\n'):
                line = line.strip()
                # 跳过空行
                if not line:
                    continue
                # 处理"""单行注释
                if (line.startswith(('"""', "'''"))
                        and line.endswith(('"""', "'''"))
                        and len(line) > 3):
                    continue
                # 处理"""多行注释
                if line.startswith(('"""', "'''")):
                    in_comment = not in_comment
                    continue
                # 在注释中则跳过
                if in_comment:
                    continue
                # 跳过#注释、pass语句、装饰器、函数定义行
                if (line.startswith('#')
                        or line == "pass"
                        or line.startswith('@')
                        or line.startswith('def ')):
                    continue
                # 发现有效代码行
                return True
            # 没有有效代码行
            return False
        except Exception as err:
            print(err)
            # 源代码分析失败时，进行字节码分析
            code_obj = func.__code__
            instructions = list(dis.get_instructions(code_obj))
            # 检查是否为仅返回None的简单结构
            if len(instructions) == 2:
                first, second = instructions
                if (first.opname == 'LOAD_CONST' and
                        second.opname == 'RETURN_VALUE'):
                    # 验证加载的常量是否为None
                    const_index = first.arg
                    if (const_index < len(code_obj.co_consts) and
                            code_obj.co_consts[const_index] is None):
                        # 未实现的空函数
                        return False
            # 其他情况认为已实现
            return True

    @staticmethod
    def check_signature(func: FunctionType, *args) -> bool:
        """
        检查输出与函数的参数类型是否一致
        """
        # 获取函数的参数信息
        signature = inspect.signature(func)
        parameters = signature.parameters
        if len(args) != len(parameters):
            return False
        try:
            # 获取解析后的类型提示
            type_hints = get_type_hints(func)
        except TypeError:
            type_hints = {}
        for arg, (param_name, param) in zip(args, parameters.items()):
            # 优先使用解析后的类型提示
            param_type = type_hints.get(param_name, None)
            if param_type is None:
                # 处理原始注解（可能为字符串或Cython类型）
                param_annotation = param.annotation
                if param_annotation is inspect.Parameter.empty:
                    continue
                # 处理字符串类型的注解
                if isinstance(param_annotation, str):
                    # 尝试解析字符串为实际类型
                    module = inspect.getmodule(func)
                    global_vars = module.__dict__ if module else globals()
                    try:
                        param_type = eval(param_annotation, global_vars)
                    except Exception as err:
                        print(str(err))
                        continue
                else:
                    param_type = param_annotation
            if param_type is None:
                continue
            if not isinstance(arg, param_type):
                return False
        return True
