"""更新自定义识别词工具"""

import json
from typing import List, Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.core.metainfo import clear_rust_parse_options_cache
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.schemas.types import SystemConfigKey


class UpdateCustomIdentifiersInput(BaseModel):
    """更新自定义识别词工具的输入参数模型"""

    identifiers: List[str] = Field(
        ...,
        description=(
            "The complete list of custom identifier rules to save. "
            "This REPLACES the entire existing list. "
            "Always query existing identifiers first, merge new rules, then pass the full list. "
            "These rules are global and affect future recognition for all torrents/files. "
            "When adding a rule for a user-provided sample, prefer narrow regex patterns that include "
            "sample-specific anchors such as the title alias, year, season/episode marker, group tag, "
            "resolution, or other distinctive fragments. Avoid overly broad patterns like bare generic "
            "tags, pure episode numbers, or common release words unless the user explicitly wants a global rule."
        ),
    )


class UpdateCustomIdentifiersTool(MoviePilotTool):
    name: str = "update_custom_identifiers"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.FilterRule,
        ToolTag.Admin,
    ]
    description: str = (
        "Update the full list of custom identifiers (自定义识别词) used for preprocessing torrent/file names. "
        "This tool REPLACES all existing identifier rules with the provided list. "
        "IMPORTANT: Always use 'query_custom_identifiers' first to get existing rules, "
        "then merge new rules into the list before calling this tool to avoid accidentally deleting existing rules. "
        "IMPORTANT: New identifier rules are global. When the rule is created from a specific torrent/file name, "
        "make the regex as narrow as possible and include distinctive elements from that sample so unrelated titles "
        "are not affected. Prefer contextual replacements with capture groups/backreferences over bare block words "
        "when a generic word like REPACK, WEB-DL, 1080p, 字幕, or a simple episode marker would otherwise match too broadly. "
        "Supported rule formats (spaces around operators are required): "
        "1) Block word: just the word/regex to remove; "
        "2) Replacement: '被替换词 => 替换词'; "
        "3) Episode offset: '前定位词 <> 后定位词 >> EP±N'; "
        "4) Combined: '被替换词 => 替换词 && 前定位词 <> 后定位词 >> EP±N'; "
        "Lines starting with '#' are comments. "
        "The replacement target supports: {[tmdbid=xxx;type=movie/tv;g=xxx;s=xxx;e=xxx]} "
        "for direct TMDB ID matching; g is an optional TMDB episode group ID for TV recognition."
    )
    require_admin: bool = True
    args_schema: Type[BaseModel] = UpdateCustomIdentifiersInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """生成友好的提示消息"""
        identifiers = kwargs.get("identifiers", [])
        return f"更新自定义识别词（共 {len(identifiers)} 条规则）"

    async def run(self, identifiers: List[str] = None, **kwargs) -> str:
        logger.info(
            f"执行工具: {self.name}, 规则数量: {len(identifiers) if identifiers else 0}"
        )
        try:
            if identifiers is None:
                return json.dumps(
                    {"success": False, "message": "必须提供 identifiers 参数"},
                    ensure_ascii=False,
                )

            # 过滤空字符串
            identifiers = [i for i in identifiers if i is not None]

            system_config_oper = SystemConfigOper()

            # 保存
            value = identifiers if identifiers else None
            success = await system_config_oper.async_set(
                SystemConfigKey.CustomIdentifiers, value
            )
            if success:
                clear_rust_parse_options_cache()
                return json.dumps(
                    {
                        "success": True,
                        "message": f"自定义识别词已更新，共 {len(identifiers)} 条规则",
                        "count": len(identifiers),
                        "identifiers": identifiers,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            else:
                return json.dumps(
                    {"success": False, "message": "保存自定义识别词失败"},
                    ensure_ascii=False,
                )
        except Exception as e:
            logger.error(f"更新自定义识别词失败: {e}")
            return json.dumps(
                {"success": False, "message": f"更新自定义识别词时发生错误: {str(e)}"},
                ensure_ascii=False,
            )
