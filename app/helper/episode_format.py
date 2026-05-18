import re
from typing import List, Match, Optional, Tuple

from app.helper.format import FormatParser
from app.log import logger
from app.schemas import EpisodeFormatRule, FileItem


class EpisodeFormatRuleHelper:
    """
    集数定位规则辅助类
    """

    def recommend(
        self,
        rules: List[EpisodeFormatRule],
        sample_files: List[FileItem],
    ) -> Tuple[bool, str, Optional[dict]]:
        """
        推荐集数定位模板
        """
        if not rules:
            return False, "未配置集数定位规则", None

        if not sample_files:
            return False, "目录中没有可用于识别的媒体文件", None

        for index, rule in enumerate(rules):
            matched_samples = self._match_rule(rule, sample_files)
            if not matched_samples:
                continue

            sample_file, match_result = matched_samples[0]
            episode_format = self._build_template(sample_file.name, match_result)
            if not episode_format:
                continue
            if not self._validate_template(episode_format, matched_samples):
                logger.warn(f"集数定位规则 {rule.name} 模板校验失败")
                continue

            logger.info(
                f"集数定位规则命中：{rule.name}，样本文件：{sample_file.name}"
            )
            return True, "", {
                "rule_name": rule.name,
                "rule_index": index,
                "pattern": rule.pattern,
                "episode_format": episode_format,
                "sample_file": sample_file.name,
                "min_file_size_mb": rule.min_file_size_mb,
                "message": "已根据预定义规则生成集数定位模板",
            }

        return False, "未匹配到可用的集数定位规则", None

    @staticmethod
    def _match_rule(
        rule: EpisodeFormatRule, sample_files: List[FileItem]
    ) -> List[Tuple[FileItem, Match[str]]]:
        """
        获取规则命中的样本文件
        """
        try:
            compiled_pattern = re.compile(
                EpisodeFormatRuleHelper._normalize_pattern(rule.pattern)
            )
        except Exception as err:
            logger.warn(f"集数定位规则 {rule.name} 编译失败：{err}")
            return []

        matched_samples: List[Tuple[FileItem, Match[str]]] = []
        for item in sample_files:
            if rule.min_file_size_mb and (item.size or 0) < rule.min_file_size_mb * 1024 * 1024:
                continue
            match_result = compiled_pattern.search(item.name or "")
            if not match_result or "ep" not in match_result.groupdict():
                continue
            matched_samples.append((item, match_result))
        return matched_samples

    def _build_template(self, file_name: str, match_result: Match[str]) -> Optional[str]:
        """
        根据命中的样本生成模板
        """
        group_items = []
        for group_name, group_value in match_result.groupdict().items():
            if group_value is None:
                continue
            start, end = match_result.span(group_name)
            if start < 0 or end < 0:
                continue
            group_items.append((start, end, group_name))

        if not group_items or not any(group_name == "ep" for _, _, group_name in group_items):
            return None

        group_items.sort(key=lambda item: (item[0], -(item[1] - item[0])))
        template_parts: List[str] = []
        cursor = 0
        for start, end, group_name in group_items:
            if start < cursor:
                continue
            template_parts.append(self._escape_literal(file_name[cursor:start]))
            template_parts.append(f"{{{group_name}}}")
            cursor = end
        template_parts.append(self._escape_literal(file_name[cursor:]))
        return "".join(template_parts)

    def _validate_template(
        self,
        episode_format: str,
        matched_samples: List[Tuple[FileItem, Match[str]]],
    ) -> bool:
        """
        校验生成的模板是否可被现有格式解析器稳定消费
        """
        parser = FormatParser(eformat=episode_format)
        for item, match_result in matched_samples:
            if not parser.match(item.name):
                return False
            result = parser.split_episode(file_name=item.name, file_meta=None)
            if result[0] is None:
                return False
            expected_episode = match_result.groupdict().get("ep")
            if not self._episode_matches(result[0], expected_episode):
                return False
        return True

    @staticmethod
    def _episode_matches(actual_episode: int, expected_episode: Optional[str]) -> bool:
        """
        校验模板提取出的集数是否与正则命名组一致
        """
        if expected_episode is None:
            return False
        number_match = re.search(r"\d{1,4}", expected_episode)
        if not number_match:
            return False
        return int(number_match.group()) == actual_episode

    @staticmethod
    def _normalize_pattern(pattern: str) -> str:
        """
        将 PCRE 风格命名组转为 Python re 可识别的语法
        """
        return re.sub(r"\(\?<([a-zA-Z_][a-zA-Z0-9_]*)>", r"(?P<\1>", pattern)

    def _escape_literal(self, text: str) -> str:
        """
        将样本文本转为 parse 模板中的字面量
        """
        escaped_parts: List[str] = []
        for char in text:
            if char in "{}":
                escaped_parts.append(char * 2)
            else:
                escaped_parts.append(char)
        return "".join(escaped_parts)
