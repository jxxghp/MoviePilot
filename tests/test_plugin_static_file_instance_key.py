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

    monkeypatch.setattr(
        plugin_endpoint, "settings", SimpleNamespace(ROOT_PATH=tmp_path)
    )

    response = asyncio.run(
        plugin_endpoint.plugin_static_file(
            plugin_id="DemoPlugin@second",
            filepath="remoteEntry.js",
        )
    )

    assert response.media_type == "application/javascript"
