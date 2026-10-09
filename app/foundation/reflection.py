import ast
import dis
import importlib
import inspect
import pkgutil
import textwrap
import threading
import tokenize
import weakref
from pathlib import Path
from types import CodeType, FunctionType
from typing import Any, Callable, List, Optional, get_type_hints

FilterFuncType = Callable[[str, Any], bool]


# check_method 结果缓存：弱引用代码对象，模块或插件卸载后自动清理
_implemented_cache: "weakref.WeakKeyDictionary[CodeType, bool]" = weakref.WeakKeyDictionary()
_implemented_cache_lock = threading.Lock()


def _read_function_source(code: CodeType) -> str:
    """
    读取函数定义源码。

    与 inspect.getsource 的定位方式一致，但直接读文件而不经 linecache：
    getsource 会把整份源文件留在 linecache 中常驻，所有模块和插件累计约 10MB。
    """
    with tokenize.open(code.co_filename) as fh:
        lines = fh.readlines()
    return "".join(inspect.getblock(lines[code.co_firstlineno - 1:]))


def _is_placeholder_stmt(stmt: ast.stmt) -> bool:
    """pass、docstring、... 与 raise NotImplementedError 视为未实现的占位语句。"""
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
        return isinstance(stmt.value.value, str) or stmt.value.value is Ellipsis
    if isinstance(stmt, ast.Raise):
        exc = stmt.exc
        if isinstance(exc, ast.Call):
            exc = exc.func
        return isinstance(exc, ast.Name) and exc.id == "NotImplementedError"
    return False


def _default_filter(name: str, obj: Any) -> bool:
    """接受具有名称和值的动态加载对象。"""
    return bool(name and obj)


class ModuleHelper:
    """发现并动态加载 Python 包中的模块类。"""

    @classmethod
    def load(
        cls,
        package_path: str,
        filter_func: FilterFuncType = _default_filter,
    ) -> List[Any]:
        """加载包的一级模块，并返回通过过滤器的去重类对象。"""
        submodules: list = []
        loaded_modules = set()
        packages = importlib.import_module(package_path)
        for _, package_name, _ in pkgutil.iter_modules(packages.__path__):
            try:
                if package_name.startswith("_"):
                    continue
                full_package_name = f"{package_path}.{package_name}"
                module = importlib.import_module(full_package_name)
                importlib.reload(module)
                for name, obj in module.__dict__.items():
                    if name.startswith("_"):
                        continue
                    if isinstance(obj, type) and filter_func(name, obj):
                        if name in loaded_modules:
                            continue
                        loaded_modules.add(name)
                        submodules.append(obj)
            except Exception:
                continue
        return submodules

    @classmethod
    def load_with_pre_filter(
        cls,
        package_path: str,
        filter_func: FilterFuncType = _default_filter,
    ) -> List[Any]:
        """预检查类对象后重载所需模块，避免无关模块重复初始化。"""
        submodules = []
        packages = importlib.import_module(package_path)

        def reload_module_objects(target_module):
            """重载一个模块并返回过滤后的类对象。"""
            importlib.reload(target_module)
            return [
                obj
                for name, obj in target_module.__dict__.items()
                if not name.startswith("_")
                and isinstance(obj, type)
                and filter_func(name, obj)
            ]

        def reload_sub_modules(parent_module, parent_module_name):
            """重载父包下能够成功导入的所有子模块。"""
            for _, sub_module_name, _ in pkgutil.walk_packages(
                parent_module.__path__,
                parent_module_name + ".",
            ):
                try:
                    full_sub_module = importlib.import_module(sub_module_name)
                    importlib.reload(full_sub_module)
                except Exception:
                    continue

        for _, package_name, is_pkg in pkgutil.iter_modules(packages.__path__):
            if package_name.startswith("_"):
                continue
            full_package_name = f"{package_path}.{package_name}"
            try:
                module = importlib.import_module(full_package_name)
                candidates = [
                    (name, obj)
                    for name, obj in module.__dict__.items()
                    if not name.startswith("_") and isinstance(obj, type)
                ]
                if any(filter_func(name, obj) for name, obj in candidates):
                    if is_pkg:
                        reload_sub_modules(module, full_package_name)
                    submodules.extend(reload_module_objects(module))
            except Exception:
                continue
        return submodules

    @staticmethod
    def dynamic_import_all_modules(base_path: Path, package_name: str) -> None:
        """动态导入指定目录下的全部一级 Python 模块。"""
        for file in base_path.glob("*.py"):
            file_name = file.stem
            if file_name != "__init__":
                importlib.import_module(f"{package_name}.{file_name}")


class ObjectUtils:
    """提供对象类型、函数实现和签名检查能力。"""

    @staticmethod
    def is_obj(obj: Any):
        """判断值是否属于可展开的复合对象。"""
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
        """判断字符串是否以常见复合对象字面量开头。"""
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
    def check_method(func: Callable[..., Any]) -> bool:
        """
        检查函数是否已实现

        模块链每次分发都会调用，结果按代码对象缓存；插件热重载生成新代码对象后自然重新判定，
        旧代码对象回收时缓存项随之释放。
        """
        target = inspect.unwrap(func)
        code = getattr(getattr(target, "__func__", target), "__code__", None)
        if code is None:
            return ObjectUtils._check_method_uncached(func, None)
        cached = _implemented_cache.get(code)
        if cached is None:
            cached = ObjectUtils._check_method_uncached(func, code)
            with _implemented_cache_lock:
                _implemented_cache[code] = cached
        return cached

    @staticmethod
    def _check_method_uncached(func: Callable[..., Any], code: Optional[CodeType]) -> bool:
        """按源码语法判断函数体是否只有占位语句，源码不可得时退回字节码判断。"""
        try:
            if code is None:
                raise OSError("callable without code object")
            tree = ast.parse(textwrap.dedent(_read_function_source(code)))
            node = tree.body[0]
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return True
            return not all(_is_placeholder_stmt(stmt) for stmt in node.body)
        except Exception:
            # 源代码分析失败时，进行字节码分析
            code_obj = func.__code__  # type: ignore[attr-defined]
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
                    except Exception:
                        continue
                else:
                    param_type = param_annotation
            if param_type is None:
                continue
            if not isinstance(arg, param_type):
                return False
        return True
