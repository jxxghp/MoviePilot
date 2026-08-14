import ast
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.generate_plugin_market_default import (
    OFFICIAL_PLUGIN_MARKET,
    _extract_plugin_market_repos_from_wiki,
    _generate_plugin_market_default,
)


ROOT_DIR = Path(__file__).resolve().parents[1]


def _read_plugin_market_default(config_file: Path) -> str:
    tree = ast.parse(config_file.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "ConfigModel":
            continue
        for item in node.body:
            if not isinstance(item, ast.AnnAssign):
                continue
            if isinstance(item.target, ast.Name) and item.target.id == "PLUGIN_MARKET":
                return ast.literal_eval(item.value)
    raise AssertionError("未找到 PLUGIN_MARKET 默认值")


def test_extract_plugin_market_repos_uses_marked_section_and_deduplicates() -> None:
    """
    Wiki 清单解析只读取标记区域，并规范化、去重仓库地址。
    """
    markdown = """
- https://github.com/outside/ignored
<!-- plugin-market-repos:start -->
- https://github.com/jxxghp/MoviePilot-Plugins/
- https://github.com/demo/Market.git
- https://github.com/demo/Market
<!-- plugin-market-repos:end -->
- https://github.com/outside/ignored-again
"""

    assert _extract_plugin_market_repos_from_wiki(markdown) == [
        OFFICIAL_PLUGIN_MARKET,
        "https://github.com/demo/Market",
    ]


def test_extract_plugin_market_repos_requires_unique_markers_for_build() -> None:
    """
    构建模式拒绝缺失或重复边界标记的 Wiki 文档。
    """
    with pytest.raises(ValueError, match="唯一且有序的开始和结束标记"):
        _extract_plugin_market_repos_from_wiki(f"- {OFFICIAL_PLUGIN_MARKET}")

    markdown = f"""
<!-- plugin-market-repos:start -->
<!-- plugin-market-repos:start -->
- {OFFICIAL_PLUGIN_MARKET}
<!-- plugin-market-repos:end -->
"""
    with pytest.raises(ValueError, match="唯一且有序的开始和结束标记"):
        _extract_plugin_market_repos_from_wiki(markdown)


def test_generate_plugin_market_default_updates_assignment_idempotently(
    tmp_path: Path,
) -> None:
    """
    生成脚本只替换 ConfigModel 默认值，并保持重复执行结果一致。
    """
    wiki_file = tmp_path / "plugin.md"
    wiki_file.write_text(
        f"""
<!-- plugin-market-repos:start -->
- {OFFICIAL_PLUGIN_MARKET}
- https://github.com/demo/MoviePilot-Plugins
<!-- plugin-market-repos:end -->
""",
        encoding="utf-8",
    )
    config_file = tmp_path / "config.py"
    config_file.write_text(
        """class ConfigModel(BaseModel):
    PLUGIN_MARKET: str = "https://github.com/old/Market"
    OTHER_SETTING: bool = True
""",
        encoding="utf-8",
    )

    repos = _generate_plugin_market_default(wiki_file, config_file)
    first_result = config_file.read_text(encoding="utf-8")
    _generate_plugin_market_default(wiki_file, config_file)

    assert repos == [
        OFFICIAL_PLUGIN_MARKET,
        "https://github.com/demo/MoviePilot-Plugins",
    ]
    assert _read_plugin_market_default(config_file) == ",".join(repos)
    assert "OTHER_SETTING: bool = True" in first_result
    assert config_file.read_text(encoding="utf-8") == first_result


def test_generate_plugin_market_default_requires_official_repo(
    tmp_path: Path,
) -> None:
    """
    发版默认清单缺少官方仓库时终止生成。
    """
    wiki_file = tmp_path / "plugin.md"
    wiki_file.write_text(
        """
<!-- plugin-market-repos:start -->
- https://github.com/demo/MoviePilot-Plugins
<!-- plugin-market-repos:end -->
""",
        encoding="utf-8",
    )
    config_file = tmp_path / "config.py"
    config_file.write_text(
        """class ConfigModel(BaseModel):
    PLUGIN_MARKET: str = "https://github.com/old/Market"
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="缺少 MoviePilot 官方插件仓库"):
        _generate_plugin_market_default(wiki_file, config_file)


def test_generate_plugin_market_default_runs_without_project_dependencies(
    tmp_path: Path,
) -> None:
    """发版脚本在禁用 site-packages 时仍应能独立生成默认配置。"""
    wiki_file = tmp_path / "plugin.md"
    wiki_file.write_text(
        f"""
<!-- plugin-market-repos:start -->
- {OFFICIAL_PLUGIN_MARKET}
<!-- plugin-market-repos:end -->
""",
        encoding="utf-8",
    )
    config_file = tmp_path / "config.py"
    config_file.write_text(
        'class ConfigModel:\n    PLUGIN_MARKET: str = "old"\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-m",
            "scripts.generate_plugin_market_default",
            "--wiki-file",
            str(wiki_file),
            "--config-file",
            str(config_file),
        ],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert _read_plugin_market_default(config_file) == OFFICIAL_PLUGIN_MARKET


def test_release_workflows_use_canonical_config_path() -> None:
    """正式版和 Beta 发版流程必须修改并暂存 canonical 配置文件。"""
    build_workflow = (
        ROOT_DIR / ".github" / "workflows" / "build-v3.yml"
    ).read_text(encoding="utf-8")
    beta_workflow = (
        ROOT_DIR / ".github" / "workflows" / "beta.yml"
    ).read_text(encoding="utf-8")

    assert "--config-file app/runtime/config.py" in build_workflow
    assert "git add app/runtime/config.py" in build_workflow
    assert "--config-file app/runtime/config.py" in beta_workflow
    assert "app/core/config.py" not in build_workflow
    assert "app/core/config.py" not in beta_workflow
