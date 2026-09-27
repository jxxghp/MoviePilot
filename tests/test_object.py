import functools
import linecache

from app.foundation import reflection
from app.foundation.reflection import ObjectUtils


def test_check_method():
    def implemented_function():
        return "Hello"

    def pass_function():
        pass

    def docstring_function():
        """This is a docstring."""

    def ellipsis_function():
        ...

    def not_implemented_function():
        raise NotImplementedError

    def not_implemented_function_with_call():
        raise NotImplementedError()

    async def multiple_lines_async_def(_param1: str,
                                       _param2: str):
        pass

    def empty_function():
        return

    assert ObjectUtils.check_method(implemented_function)
    assert not ObjectUtils.check_method(pass_function)
    assert not ObjectUtils.check_method(docstring_function)
    assert not ObjectUtils.check_method(ellipsis_function)
    assert not ObjectUtils.check_method(not_implemented_function)
    assert not ObjectUtils.check_method(not_implemented_function_with_call)
    assert not ObjectUtils.check_method(multiple_lines_async_def)
    assert ObjectUtils.check_method(empty_function)


class _Module:
    """模拟模块类：占位方法与已实现方法并存，并经装饰器包装。"""

    def placeholder(self):
        """未实现"""

    @staticmethod
    def _wrap(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        return wrapper

    @_wrap
    def decorated_placeholder(self):
        pass

    def implemented(self):
        return 1


def test_check_method_handles_bound_and_decorated_methods():
    module = _Module()

    assert not ObjectUtils.check_method(module.placeholder)
    assert not ObjectUtils.check_method(module.decorated_placeholder)
    assert ObjectUtils.check_method(module.implemented)


def test_check_method_does_not_leave_source_in_linecache():
    linecache.clearcache()

    def pass_function():
        pass

    assert not ObjectUtils.check_method(pass_function)
    assert __file__ not in linecache.cache


def test_check_method_caches_result_per_code_object(monkeypatch):
    calls = []
    original = reflection._read_function_source
    monkeypatch.setattr(
        reflection, "_read_function_source", lambda code: calls.append(code) or original(code)
    )

    def make():
        def pass_function():
            pass

        return pass_function

    # 两个闭包共享同一代码对象，只读取一次源码
    first, second = make(), make()
    assert not ObjectUtils.check_method(first)
    assert not ObjectUtils.check_method(second)
    assert calls == [first.__code__]
