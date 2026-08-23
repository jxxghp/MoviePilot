from pathlib import Path

from app.runtime.config import settings
from app.adapters.system.resource import (
    ResourceHelper,
    configure_resource_version_provider,
)
from app.startup import modules_initializer as modules_initializer


ROOT_DIR = Path(__file__).resolve().parents[1]


def test_resource_helper_uses_v3_only():
    """在线资源更新器必须只请求 V3 清单、目录和站点索引文件。"""
    assert settings.VERSION_FLAG == "v3"
    assert settings.RESOURCE_VERSION_FLAG == "v3"
    assert ResourceHelper._repo.endswith("/package.v3.json")
    assert ResourceHelper._files_api.endswith("/resources.v3")
    assert ResourceHelper._get_needed_files()[0] == "user.sites.v3.bin"
    assert ResourceHelper._resource_target == Path("app/application/site")


def test_resource_helper_preserves_no_argument_check_contract(monkeypatch):
    """旧插件无参数调用 check 时应使用启动层注入版本，不反向导入站点应用。"""
    provider_calls = []

    def provide_versions() -> tuple[str, str]:
        """记录资源更新器是否读取了组合根注入的版本。"""
        provider_calls.append(True)
        return "2.4.10", "3.0.2"

    configure_resource_version_provider(provide_versions)
    monkeypatch.setattr(ResourceHelper, "_load_resource_info", lambda _self: None)
    monkeypatch.setattr("app.adapters.system.resource.settings.AUTO_UPDATE_RESOURCE", True)
    monkeypatch.setattr(
        "app.adapters.system.resource.SystemUtils.is_frozen",
        lambda: False,
    )

    assert ResourceHelper().check() is False
    assert provider_calls == [True]


def test_startup_owns_restart_after_resource_update(monkeypatch):
    """资源适配器只返回更新结果，进程重启必须由启动组合层触发。"""
    restart_calls = []
    monkeypatch.setattr(
        modules_initializer,
        "ResourceHelper",
        lambda: type(
            "ResourceStub",
            (),
            {"check": lambda self, **_versions: True},
        )(),
    )
    monkeypatch.setattr(
        modules_initializer.SystemHelper,
        "restart",
        lambda: restart_calls.append(True) or (True, "ok"),
    )

    modules_initializer.update_resources()

    assert restart_calls == [True]


def test_startup_does_not_restart_without_resource_update(monkeypatch):
    """没有成功安装新资源时启动层不得请求重启。"""
    monkeypatch.setattr(
        modules_initializer,
        "ResourceHelper",
        lambda: type(
            "ResourceStub",
            (),
            {"check": lambda self, **_versions: False},
        )(),
    )
    monkeypatch.setattr(
        modules_initializer.SystemHelper,
        "restart",
        lambda: (_ for _ in ()).throw(AssertionError("不应重启")),
    )

    modules_initializer.update_resources()


def test_install_and_docker_paths_do_not_reference_v2_resources():
    """本地安装和 Docker 资源流程不得包含 V2 资源回退。"""
    paths = [
        ROOT_DIR / "scripts" / "local_setup.py",
        ROOT_DIR / "docker" / "Dockerfile",
        ROOT_DIR / "docker" / "update.sh",
    ]

    for path in paths:
        content = path.read_text(encoding="utf-8")
        assert "resources.v2" not in content
        assert "user.sites.v2.bin" not in content
        assert "resources.v3" in content
        assert "app/application/site" in content


def test_docker_entrypoint_does_not_sync_updater_as_a_special_case():
    """容器控制脚本必须由 launcher 统一固化，不能单独替换 updater。"""
    content = (ROOT_DIR / "docker" / "entrypoint.sh").read_text(encoding="utf-8")

    assert "mp_update.sh" not in content
    assert 'source "${MP_CONTROL_DIR:-/usr/local/lib/moviepilot/control}/update.sh"' in content


def test_v3_release_workflows_use_main_wiki_and_isolated_images():
    """V3 正式版和 Beta 构建应读取主 Wiki 分支并保持镜像仓库隔离。"""
    build_workflow = (ROOT_DIR / ".github" / "workflows" / "build-v3.yml").read_text(encoding="utf-8")
    beta_workflow = (ROOT_DIR / ".github" / "workflows" / "beta.yml").read_text(encoding="utf-8")

    assert "name: MoviePilot Builder v3" in build_workflow
    assert "      - v3" in build_workflow
    assert "          repository: jxxghp/MoviePilot-Wiki\n          ref: main" in build_workflow
    assert "${{ secrets.DOCKER_USERNAME }}/moviepilot-v3" in build_workflow
    assert "ghcr.io/${{ github.repository }}-v3" in build_workflow
    assert "${{ secrets.DOCKER_USERNAME }}/moviepilot-v2" not in build_workflow
    assert "${{ secrets.DOCKER_USERNAME }}/moviepilot\n" not in build_workflow
    assert "git tag -l 'v3.*'" in build_workflow

    assert "          repository: jxxghp/MoviePilot-Wiki\n          ref: main" in beta_workflow
    assert "${{ secrets.DOCKER_USERNAME }}/moviepilot-v3" in beta_workflow
    assert "ghcr.io/${{ github.repository }}-v3" in beta_workflow
    assert "${{ secrets.DOCKER_USERNAME }}/moviepilot-v2" not in beta_workflow
