import logging
import re
from typing import TypedDict

import yaml

logger = logging.getLogger(__name__)

MAX_SKILL_CONTENT_BYTES = 512 * 1024
MAX_SKILL_NAME_LENGTH = 64
MAX_SKILL_DESCRIPTION_LENGTH = 1024
MAX_SKILL_COMPATIBILITY_LENGTH = 500


class SkillMetadata(TypedDict):
    """描述符合 Agent Skills 规范的已校验元数据。"""

    path: str
    id: str
    name: str
    version: int
    description: str
    license: str | None
    compatibility: str | None
    metadata: dict[str, str]
    allowed_tools: list[str]
    allowed_api_operations: list[str]


def _validate_metadata(raw: object, skill_path: str) -> dict[str, str]:
    """将 YAML metadata 字段规范化为字符串键值映射。"""
    if not isinstance(raw, dict):
        if raw:
            logger.warning(
                "Ignoring non-dict metadata in %s (got %s)",
                skill_path,
                type(raw).__name__,
            )
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def parse_skill_metadata(  # noqa: C901
    content: str,
    skill_path: str,
    skill_id: str,
) -> SkillMetadata | None:
    """解析并校验一个 SKILL.md 的 YAML 前言。"""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        logger.warning("Skipping %s: no valid YAML frontmatter found", skill_path)
        return None

    try:
        frontmatter_data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as err:
        logger.warning("Invalid YAML in %s: %s", skill_path, err)
        return None
    if not isinstance(frontmatter_data, dict):
        logger.warning("Skipping %s: frontmatter is not a mapping", skill_path)
        return None

    name = str(frontmatter_data.get("name", "")).strip()
    description = str(frontmatter_data.get("description", "")).strip()
    if not name or not description:
        logger.warning("Skipping %s: missing required 'name' or 'description'", skill_path)
        return None
    if len(description) > MAX_SKILL_DESCRIPTION_LENGTH:
        logger.warning(
            "Description exceeds %d characters in %s, truncating",
            MAX_SKILL_DESCRIPTION_LENGTH,
            skill_path,
        )
        description = description[:MAX_SKILL_DESCRIPTION_LENGTH]

    raw_tools = frontmatter_data.get("allowed-tools")
    if isinstance(raw_tools, str):
        allowed_tools = [tool.strip(",") for tool in raw_tools.split() if tool.strip(",")]
    else:
        if raw_tools is not None:
            logger.warning(
                "Ignoring non-string 'allowed-tools' in %s (got %s)",
                skill_path,
                type(raw_tools).__name__,
            )
        allowed_tools = []

    raw_api_operations = frontmatter_data.get("allowed-api-operations")
    if isinstance(raw_api_operations, str):
        allowed_api_operations = [
            operation.strip(",") for operation in raw_api_operations.split() if operation.strip(",")
        ]
    else:
        if raw_api_operations is not None:
            logger.warning(
                "Ignoring non-string 'allowed-api-operations' in %s (got %s)",
                skill_path,
                type(raw_api_operations).__name__,
            )
        allowed_api_operations = []

    compatibility = str(frontmatter_data.get("compatibility", "")).strip() or None
    if compatibility and len(compatibility) > MAX_SKILL_COMPATIBILITY_LENGTH:
        logger.warning(
            "Compatibility exceeds %d characters in %s, truncating",
            MAX_SKILL_COMPATIBILITY_LENGTH,
            skill_path,
        )
        compatibility = compatibility[:MAX_SKILL_COMPATIBILITY_LENGTH]

    raw_version = frontmatter_data.get("version")
    version = 0
    if raw_version is not None:
        try:
            version = int(raw_version)
        except (ValueError, TypeError):
            logger.warning(
                "Invalid 'version' in %s (got %r), defaulting to 0",
                skill_path,
                raw_version,
            )

    return SkillMetadata(
        id=skill_id,
        name=name,
        version=version,
        description=description,
        path=skill_path,
        metadata=_validate_metadata(frontmatter_data.get("metadata", {}), skill_path),
        license=str(frontmatter_data.get("license", "")).strip() or None,
        compatibility=compatibility,
        allowed_tools=allowed_tools,
        allowed_api_operations=allowed_api_operations,
    )
