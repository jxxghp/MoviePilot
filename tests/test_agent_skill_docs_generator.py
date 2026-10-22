"""
验证技能文档保留英文操作指南与中文源契约，并可重复生成。
"""

import re
import runpy
import shutil
from pathlib import Path

import pytest

from scripts import generate_agent_skill_docs as generator


@pytest.fixture(scope="module")
def api_categories() -> dict[str, str]:
    """
    从真实 MCP 元数据生成分类文档，供契约断言复用。
    """
    categories, _, _ = generator._split_api_docs(generator._render_api_docs())
    return categories


@pytest.mark.parametrize(
    ("category", "operation", "field"),
    [
        ("search", "search.torrents", "mtype"),
        ("site", "site.searchable", "media_type*"),
        ("site", "site.resource", "mtype"),
        ("filter", "filter.group.add", "media_type"),
        ("subtitle", "subtitle.search.media", "mtype"),
    ],
)
def test_agent_media_keys_are_scoped_to_supported_operations(
    api_categories: dict[str, str], category: str, operation: str, field: str
) -> None:
    """
    仅在接口接受 Agent 媒体键时显示英文取值。
    """
    section = api_categories[category].split(f"### `{operation}`\n", 1)[1].split("\n### `", 1)[0]
    assert f"`{field}` (string(movie,tv,music)" in section


def test_distinct_media_type_contracts_are_preserved(api_categories: dict[str, str]) -> None:
    """
    推荐的 all、订阅枚举和数据库过滤字段不得被同名字段覆盖。
    """
    assert "`media_type` (string(all,movie,tv,music); default `all`)" in api_categories["recommendation"]
    assert "`mtype` (MediaType|null)" in api_categories["subscription"]
    assert "`mtype*` (string):" in api_categories["subscription"]
    assert "`mtype` (string|null):" in api_categories["library"]
    assert "Agent-facing values" not in api_categories["subscription"]
    assert "Agent-facing values" not in api_categories["library"]


def test_regeneration_retains_operation_guidance(api_categories: dict[str, str]) -> None:
    """
    生成器必须保留无法从原始 schema 得出的业务和超时说明。
    """
    assert "Music uses exact identity only." in api_categories["subscription"]
    assert "paused subscription without enabling it" in api_categories["subscription"]
    assert "Omitted fields keep their stored values." in api_categories["subscription"]
    assert "MUSIC_METADATA_TO_SIMPLIFIED" in api_categories["music"]
    assert "290 seconds" in api_categories["search"]
    assert "300 seconds" in api_categories["search"]
    assert "600 seconds by default" in api_categories["search"]
    assert "context containing `media_info`, not a candidate list" in api_categories["media"]
    assert "unavailable source does not prove there are no resources" in api_categories["search"]


def test_full_regeneration_is_current_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    在临时目录重建全部技能，验证已提交产物同步且重复执行无变化。
    """
    source_root = generator.PROJECT_ROOT
    shutil.copytree(source_root / "skills", tmp_path / "skills")
    schema_path = Path("app/agent/policy/resources/api_mcp_schema.json")
    (tmp_path / schema_path).parent.mkdir(parents=True)
    shutil.copyfile(source_root / schema_path, tmp_path / schema_path)
    monkeypatch.setattr(generator, "PROJECT_ROOT", tmp_path)
    expected = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in (tmp_path / "skills").rglob("*.md")
    }
    database_skill = tmp_path / "skills/database-operation/SKILL.md"
    database_skill.write_text(re.sub(r"(?m)^version: \d+$", "version: 11",
                                    database_skill.read_text(encoding="utf-8"), count=1), encoding="utf-8")

    for _ in range(2):
        assert generator.main() == 0
        actual = {
            path.relative_to(tmp_path): path.read_bytes()
            for path in (tmp_path / "skills").rglob("*.md")
        }
        assert actual == expected
    for path in (tmp_path / "skills/moviepilot-api/api").glob("*.md"):
        guidance, marker, _ = path.read_text(encoding="utf-8").partition("## Operations\n")
        assert marker, path
        assert not re.search(r"[\u3400-\u9fff]", guidance), path


def test_api_docs_preserve_chinese_source_descriptions(api_categories: dict[str, str]) -> None:
    """
    API 契约保留源 Schema 的中文用途与字段说明，不强制翻译成英文。
    """
    schema = generator._load_json_schema()
    branch = next(
        item for item in schema["oneOf"]
        if item["properties"]["operation_id"].get("const") == "media.search"
    )
    section = api_categories["media"].split("### `media.search`\n", 1)[1].split("\n### `", 1)[0]
    purpose = branch["description"].split("请求方法：", 1)[0].strip()
    assert re.search(r"[\u3400-\u9fff]", purpose)
    assert f"Purpose: {purpose}" in section
    for field in branch["properties"]["query"]["properties"].values():
        assert re.search(r"[\u3400-\u9fff]", field["description"])
        assert field["description"] in section


@pytest.mark.parametrize("service", ["downloader", "mediaserver"])
def test_service_docs_preserve_chinese_capability_descriptions(service: str) -> None:
    """
    服务契约保留动作、参数和约束的中文能力说明，英文指南单独维护。
    """
    skill_root = generator.PROJECT_ROOT / "skills" / f"{service}-operation"
    namespace = runpy.run_path(str(skill_root / "scripts" / f"mp-{service}.py"))
    content = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    for name, spec in namespace["ACTIONS"].items():
        contract = spec.to_dict(name)
        assert re.search(r"[\u3400-\u9fff]", contract["description"])
        assert contract["description"] in content
        for argument in contract["arguments"]:
            assert re.search(r"[\u3400-\u9fff]", argument["description"])
            assert argument["description"] in content
        for rule in contract.get("argument_rules") or []:
            assert rule in content
