import inspect
from unittest import TestCase

from app.core.thread import ThreadHelper


class CoreThreadRelocationTest(TestCase):
    """ThreadHelper 迁移至 app.core.thread：核心可用、helper 垫片兼容、core 不再反向依赖 helper.thread。"""

    def test_helper_shim_reexports_same_class(self):
        """app.helper.thread 作为兼容垫片应 re-export 同一个类。"""
        from app.core.thread import ThreadHelper as CoreTH
        from app.helper.thread import ThreadHelper as HelperTH
        self.assertIs(CoreTH, HelperTH)

    def test_submit_executes(self):
        """迁移后的线程池仍能正常提交并执行任务。"""
        future = ThreadHelper().submit(lambda x: x + 1, 41)
        self.assertEqual(future.result(timeout=5), 42)

    def test_core_event_no_longer_imports_helper_thread(self):
        """core/event 不应再从 helper 反向导入 ThreadHelper。"""
        import app.core.event as event_mod
        self.assertNotIn("app.helper.thread", inspect.getsource(event_mod))

    def test_core_thread_has_no_helper_dependency(self):
        """新的 core.thread 自身不应依赖 helper 层。"""
        import app.core.thread as thread_mod
        self.assertNotIn("app.helper", inspect.getsource(thread_mod))
