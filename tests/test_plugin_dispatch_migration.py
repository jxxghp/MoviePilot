"""插件挂载废弃分发名与新多来源契约名的启动期提示。"""

from types import SimpleNamespace

from app.runtime.extensions.projection.plugin import PluginProjection


class _Plugin(SimpleNamespace):
    """提供可配置插件 hook 的最小运行态插件替身。"""

    def __init__(self, enabled=True, **hooks):
        """保存启用状态、插件名称和 hook 实现。"""
        super().__init__(plugin_name=hooks.pop("plugin_name", "测试插件"), **hooks)
        self._enabled = enabled

    def get_state(self):
        """返回预设启用状态。"""
        return self._enabled

    def get_name(self):
        """返回插件展示名称。"""
        return self.plugin_name


class _RecordingLog(SimpleNamespace):
    """收集各级别日志调用参数，供断言文案内容。"""

    def __init__(self):
        super().__init__(warnings=[], infos=[], errors=[])

    def warning(self, message):
        self.warnings.append(message)

    def info(self, message):
        self.infos.append(message)

    def error(self, message):
        self.errors.append(message)


def test_deprecated_method_name_triggers_warning_with_plugin_id_and_replacement():
    """挂载废弃分发名时，警告需点名插件 ID、旧键与应改用的新契约名。"""
    log = _RecordingLog()
    plugin = _Plugin(get_module=lambda: {"tmdb_info": lambda: None})
    projection = PluginProjection({"DemoDeprecated": plugin}, log=log)

    projection.modules()

    assert len(log.warnings) == 1
    warning = log.warnings[0]
    assert "DemoDeprecated" in warning
    assert "tmdb_info" in warning
    assert "media_detail" in warning
    assert not log.errors


def test_new_contract_method_name_does_not_trigger_deprecated_warning():
    """挂载新契约名不应触发废弃告警，只可能触发多来源自认领提示。"""
    log = _RecordingLog()
    plugin = _Plugin(get_module=lambda: {"media_detail": lambda: None})
    projection = PluginProjection({"DemoNewContract": plugin}, log=log)

    projection.modules()

    assert not log.warnings
    assert len(log.infos) == 1
    assert "DemoNewContract" in log.infos[0]
    assert "media_detail" in log.infos[0]


def test_exempt_single_source_accessor_triggers_no_warning():
    """单源原生访问器（如 tmdb_collection）不属于废弃名，不应告警。"""
    log = _RecordingLog()
    plugin = _Plugin(get_module=lambda: {"tmdb_collection": lambda: None})
    projection = PluginProjection({"DemoExempt": plugin}, log=log)

    projection.modules()

    assert not log.warnings
    assert not log.infos
    assert not log.errors


def test_warning_does_not_rewrite_method_table():
    """告警只读不写：投影出的方法表与告警前完全一致。"""
    log = _RecordingLog()
    handler = lambda: None
    table = {"douban_info": handler}
    plugin = _Plugin(get_module=lambda: table)
    projection = PluginProjection({"DemoReadOnly": plugin}, log=log)

    result = projection.modules()

    assert log.warnings
    assert result == {("DemoReadOnly", "测试插件"): {"douban_info": handler}}
    assert result[("DemoReadOnly", "测试插件")] is table


def test_repeated_modules_calls_do_not_repeat_same_plugin_method_warning():
    """modules() 高频调用时，同一插件同一废弃键只警告一次。"""
    log = _RecordingLog()
    plugin = _Plugin(get_module=lambda: {"movie_top250": lambda: None})
    projection = PluginProjection({"DemoRepeat": plugin}, log=log)

    projection.modules()
    projection.modules()
    projection.modules()

    assert len(log.warnings) == 1


def test_module_type_enum_importable_with_all_members():
    """ModuleType 枚举符号恢复可导入，且成员齐全。

    ``Auth`` 没有对应的内建模块：它是登录认证服务族的能力标签，取值与其余服务族同出
    一套词表，因此登记在这个已知值目录里而不另起一套。
    """
    from app.schemas.types import ModuleType

    names = {member.name for member in ModuleType}
    assert names == {
        "Downloader",
        "MediaServer",
        "Notification",
        "MediaRecognize",
        "Indexer",
        "Storage",
        "Auth",
        "Other",
    }
