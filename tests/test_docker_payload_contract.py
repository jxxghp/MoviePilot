"""Docker 构建输入和镜像载荷分层合同。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker" / "Dockerfile"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "build-v3.yml"
BETA_WORKFLOW = ROOT / ".github" / "workflows" / "beta.yml"


def _read(path: Path) -> str:
    """读取构建合同文件。"""
    return path.read_text(encoding="utf-8")


def test_build_context_excludes_runtime_state_and_keeps_release_inputs() -> None:
    """本地运行数据不得进入构建上下文，发布所需入口必须保留。"""
    dockerignore = _read(ROOT / ".dockerignore")

    for pattern in (
        ".venv/",
        ".worktrees/",
        ".build/",
        ".agent-work/",
        ".runtime/",
        ".tmp/",
        "node_modules/",
        "public/",
        ".moviepilot.env",
        ".env",
        ".env.*",
        "**/*.pyc",
        "**/__pycache__/",
        "**/*.pyd",
        "**/*.so",
        "coverage.json",
        "coverage.xml",
        "config/*",
        "app/plugins/**",
        "app/application/site/*.bin",
    ):
        assert pattern in dockerignore

    assert "!config/category.yaml" in dockerignore
    assert "!app/plugins/__init__.py" in dockerignore
    assert "frontend-dist" not in dockerignore


def test_dockerfile_assigns_each_payload_to_an_independent_stage() -> None:
    """高频载荷必须由独立 stage 生成，并在 final 中分别写入目标目录。"""
    dockerfile = _read(DOCKERFILE)

    for stage in (
        "prepare_backend",
        "prepare_frontend",
        "prepare_plugins",
        "prepare_resources",
        "prepare_control",
    ):
        assert f" AS {stage}" in dockerfile

    final_start = dockerfile.index("FROM prepare_package AS final")
    final = dockerfile[final_start:]
    copies = (
        "COPY --link --from=prepare_frontend /public /public",
        "COPY --link --from=prepare_plugins /plugins /app/app/plugins",
        "COPY --link --from=prepare_resources /resources /app/app/application/site",
        "COPY --link --from=prepare_backend /app /app",
    )
    positions = [final.index(copy) for copy in copies]

    assert positions == sorted(positions)
    for copy in copies:
        assert final.count(copy) == 1
    assert "COPY --from=prepare_control /bundle/entrypoint.sh /entrypoint.sh" in final
    assert "RUN rm -rf /app/frontend-dist" in dockerfile


def test_plugin_runtime_updates_preserve_legacy_base_entrypoint() -> None:
    """更新和性能覆盖镜像必须保留旧插件导入 _PluginBase 所需的兼容入口。"""
    update_script = _read(ROOT / "docker" / "update.sh")
    perf_script = _read(ROOT / "scripts" / "perf" / "moviepilot_docker_ab.py")

    assert 'rm -f "${stage_plugin_dir}/__init__.py"' not in update_script
    assert "rm -f /frozen/plugins/__init__.py" not in perf_script


def test_release_workflows_pin_and_record_external_payload_identities() -> None:
    """正式与 Beta 构建都必须以真实制品身份驱动缓存并写入镜像标签。"""
    for workflow_path in (RELEASE_WORKFLOW, BETA_WORKFLOW):
        workflow = _read(workflow_path)

        for build_arg in (
            "MOVIEPILOT_FRONTEND_VERSION=",
            "MOVIEPILOT_FRONTEND_SHA256=",
            "MOVIEPILOT_PLUGINS_REF=",
            "MOVIEPILOT_RESOURCES_REF=",
        ):
            assert build_arg in workflow

        for label in (
            "org.moviepilot.source-revision=",
            "org.moviepilot.frontend-version=",
            "org.moviepilot.frontend-digest=",
            "org.moviepilot.plugins-revision=",
            "org.moviepilot.resources-revision=",
            "org.moviepilot.plugin-market-wiki-revision=",
            "org.moviepilot.models-catalog-digest=",
        ):
            assert label in workflow

        assert "git ls-remote https://github.com/jxxghp/MoviePilot-Plugins.git" in workflow
        assert "git ls-remote https://github.com/jxxghp/MoviePilot-Resources.git" in workflow
        assert "sha256:*) frontend_sha256=" in workflow
        assert "^[0-9a-f]{40}$" in workflow
        assert "^[0-9a-f]{64}$" in workflow


def test_same_version_rebuild_identity_is_not_derived_from_image_tag() -> None:
    """重复发布同一版本时，缓存身份必须来自源码和制品而不是镜像 Tag。"""
    release_workflow = _read(RELEASE_WORKFLOW)

    assert "SOURCE_COMMIT=$(git rev-parse HEAD)" in release_workflow
    assert "org.moviepilot.source-revision=${{ env.SOURCE_COMMIT }}" in release_workflow
    assert (
        "org.moviepilot.release-snapshot-revision="
        "${{ steps.release_snapshot.outputs.release_commit }}" in release_workflow
    )
    assert "type=raw,value=${{ env.app_version }}" in release_workflow
    assert "MOVIEPILOT_PLUGINS_REF=${{ steps.payloads.outputs.plugins_revision }}" in release_workflow
    assert "MOVIEPILOT_RESOURCES_REF=${{ steps.payloads.outputs.resources_revision }}" in release_workflow


def test_custom_frontend_directory_is_stable_but_artifacts_remain_untracked() -> None:
    """干净 checkout 可直接构建，自定义前端产物仍保持本地状态。"""
    gitignore = _read(ROOT / ".gitignore")
    dockerfile = _read(DOCKERFILE)

    assert (ROOT / "frontend-dist" / ".gitkeep").is_file()
    assert "frontend-dist/*" in gitignore
    assert "!frontend-dist/.gitkeep" in gitignore
    assert "COPY frontend-dist/ /tmp/frontend-dist/" in dockerfile
    assert "! -name '.gitkeep'" in dockerfile
