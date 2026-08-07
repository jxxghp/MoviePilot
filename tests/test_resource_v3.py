from pathlib import Path

from app.core.config import settings
from app.helper.resource import ResourceHelper


ROOT_DIR = Path(__file__).resolve().parents[1]


def test_resource_helper_uses_v3_only():
    """在线资源更新器必须只请求 V3 清单、目录和站点索引文件。"""
    assert settings.VERSION_FLAG == "v3"
    assert settings.RESOURCE_VERSION_FLAG == "v3"
    assert ResourceHelper._repo.endswith("/package.v3.json")
    assert ResourceHelper._files_api.endswith("/resources.v3")
    assert ResourceHelper._get_needed_files()[0] == "user.sites.v3.bin"


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


def test_v3_release_workflows_use_isolated_branches_and_images():
    """V3 正式版和 Beta 构建不得写入 V2 或无版本后缀的镜像仓库。"""
    build_workflow = (ROOT_DIR / ".github" / "workflows" / "build-v3.yml").read_text(encoding="utf-8")
    beta_workflow = (ROOT_DIR / ".github" / "workflows" / "beta.yml").read_text(encoding="utf-8")

    assert "name: MoviePilot Builder v3" in build_workflow
    assert "      - v3" in build_workflow
    assert "          ref: v3" in build_workflow
    assert "${{ secrets.DOCKER_USERNAME }}/moviepilot-v3" in build_workflow
    assert "ghcr.io/${{ github.repository }}-v3" in build_workflow
    assert "${{ secrets.DOCKER_USERNAME }}/moviepilot-v2" not in build_workflow
    assert "${{ secrets.DOCKER_USERNAME }}/moviepilot\n" not in build_workflow
    assert "git tag -l 'v3.*'" in build_workflow

    assert "          ref: v3" in beta_workflow
    assert "${{ secrets.DOCKER_USERNAME }}/moviepilot-v3" in beta_workflow
    assert "ghcr.io/${{ github.repository }}-v3" in beta_workflow
    assert "${{ secrets.DOCKER_USERNAME }}/moviepilot-v2" not in beta_workflow
