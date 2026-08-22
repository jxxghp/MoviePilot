"""插件静态文件与联邦入口路径按实例键降级的行为契约测试。

联邦构建产物属于插件本身而非某个实例，同一插件的全部实例共享同一份代码
目录；实例键请求必须先降级到插件标识才能定位到正确的目录。
"""

import asyncio
from types import SimpleNamespace

from app.api.endpoints import plugin as plugin_endpoint


def test_plugin_static_file_resolves_instance_key_to_shared_plugin_directory(
    monkeypatch, tmp_path
):
    """带实例键的请求被降级到插件标识，读取同一份共享代码目录下的文件。"""
    plugin_dir = tmp_path / "app" / "plugins" / "demoplugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "remoteEntry.js").write_text("console.log('demo')")

    # plugin.py 已迁移到 get_api_runtime_config_snapshot()（每次调用都从
    # app.runtime.config.settings 重新构建 ApiRuntimeConfig），模块层不再
    # 持有 settings 这个名字。settings.ROOT_PATH 是无 setter 的只读 property
    # （恒等于源码仓库根目录），没法 monkeypatch 实例属性；因此改为在
    # plugin_endpoint 命名空间里替换其直接 import 的
    # get_api_runtime_config_snapshot，让快照的 root_path 指向 tmp_path。
    monkeypatch.setattr(
        plugin_endpoint,
        "get_api_runtime_config_snapshot",
        lambda: SimpleNamespace(root_path=tmp_path),
    )

    response = asyncio.run(
        plugin_endpoint.plugin_static_file(
            plugin_id="DemoPlugin@second",
            filepath="remoteEntry.js",
        )
    )

    assert response.media_type == "application/javascript"
