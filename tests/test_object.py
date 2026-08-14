from unittest import TestCase

from app.foundation.reflection import ObjectUtils


class ObjectUtilsTest(TestCase):

    def test_check_method(self):
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

        self.assertTrue(ObjectUtils.check_method(implemented_function))
        self.assertFalse(ObjectUtils.check_method(pass_function))
        self.assertFalse(ObjectUtils.check_method(docstring_function))
        self.assertFalse(ObjectUtils.check_method(ellipsis_function))
        self.assertFalse(ObjectUtils.check_method(not_implemented_function))
        self.assertFalse(ObjectUtils.check_method(not_implemented_function_with_call))
        self.assertFalse(ObjectUtils.check_method(multiple_lines_async_def))
        self.assertTrue(ObjectUtils.check_method(empty_function))
