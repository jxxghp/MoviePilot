import re
from collections import defaultdict
from typing import Dict, Iterable, List, Match, Optional, Tuple

import anitopy

from app.core.config import settings
from app.helper.format import FormatParser
from app.log import logger
from app.schemas import EpisodeFormatRule, FileItem


class EpisodeFormatRuleHelper:
    """
    集数定位规则辅助类
    """

    _MIN_MEDIA_FILE_SIZE_BYTES = 100 * 1024 * 1024

    _EP_PREFIX_RE = re.compile(r"[Ee][Pp]?(\d{1,4})")
    _BRACKET_EP_RE = re.compile(r"\[(\d{1,3})\]")
    _FALLBACK_EPISODE_RE = re.compile(r"第(\d{1,4})[話话]")
    _FALLBACK_PERIOD_RE = re.compile(r"。(\d{1,4})\s")

    def recommend(
        self,
        rules: List[EpisodeFormatRule],
        sample_files: List[FileItem],
    ) -> Tuple[bool, str, Optional[dict]]:
        """
        推荐集数定位模板
        """
        if not rules:
            return self._auto_recommend(sample_files)

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

        return self._auto_recommend(sample_files)

    def _auto_recommend(
        self,
        sample_files: List[FileItem],
    ) -> Tuple[bool, str, Optional[dict]]:
        """
        自动生成集数定位模板：anitopy 反向定位 + 多文件对比
        """
        candidates = self._filter_by_extension_and_size(sample_files)
        if not candidates:
            return False, "无匹配自定义定位规则，智能生成失败", None

        valid_samples: List[Tuple[str, Tuple[int, int]]] = []
        for item in candidates:
            file_name = item.name or ""
            episode_number = None
            try:
                result = anitopy.parse(file_name)
                episode_number = result.get("episode_number")
            except Exception:
                pass
            if not episode_number:
                episode_number = self._extract_episode_fallback(file_name)
            if not episode_number:
                continue
            episode_number = str(episode_number)
            ep_span = self._locate_episode(file_name, episode_number)
            if ep_span is None:
                continue
            valid_samples.append((file_name, ep_span))

        if not valid_samples:
            return False, "无匹配自定义定位规则，智能生成失败", None

        if len(valid_samples) > 10:
            valid_samples = valid_samples[:10]

        file_names = [name for name, _ in valid_samples]
        ep_spans = [span for _, span in valid_samples]

        majority_names, majority_spans = self._select_base_file(file_names, ep_spans)

        episode_format = self._build_ep_only_template(
            majority_names, majority_spans, use_majority=False
        )

        if not self._validate_auto_template(episode_format, majority_names):
            diff_result = self._build_template_with_diff(
                majority_names, majority_spans, use_majority=False
            )
            if diff_result and self._validate_auto_template(diff_result, majority_names):
                episode_format = diff_result
            else:
                logger.warn("多文件对比未能提升模板覆盖率，使用仅 {ep} 模板")

        sample_file = majority_names[0]
        logger.info(f"智能分析生成集数定位模板：{sample_file} -> {episode_format}")

        return True, "", {
            "rule_name": "智能分析",
            "episode_format": episode_format,
            "sample_file": sample_file,
            "pattern": None,
            "message": "无匹配自定义定位规则，已智能生成（仅供参考）",
        }

    @staticmethod
    def _filter_by_extension_and_size(
        files: List[FileItem],
    ) -> List[FileItem]:
        """
        第一轮筛选：扩展名白名单 + 体积门槛（ass/ssa/mka 豁免）
        """
        SIZE_CHECK_EXEMPT_EXTENSIONS = {"ass", "ssa", "mka"}
        allowed_extensions = set()
        for ext_list in (settings.RMT_MEDIAEXT, settings.RMT_SUBEXT, settings.RMT_AUDIOEXT):
            for ext in ext_list:
                allowed_extensions.add(ext.lower().lstrip("."))
        candidates: List[FileItem] = []
        for item in files:
            ext = (item.extension or "").lower()
            if ext not in allowed_extensions:
                continue
            if ext not in SIZE_CHECK_EXEMPT_EXTENSIONS:
                if (item.size or 0) < EpisodeFormatRuleHelper._MIN_MEDIA_FILE_SIZE_BYTES:
                    continue
            candidates.append(item)
        return candidates

    @classmethod
    def _locate_episode(
        cls,
        file_name: str, episode_value: str
    ) -> Optional[Tuple[int, int]]:
        """
        三级策略反向定位 episode_number 在文件名中的位置
        """
        for m in cls._EP_PREFIX_RE.finditer(file_name):
            if m.group(1) == episode_value:
                return m.span(1)

        for m in cls._BRACKET_EP_RE.finditer(file_name):
            if m.group(1) == episode_value:
                return m.span(1)

        idx = file_name.rfind(episode_value)
        if idx >= 0:
            return (idx, idx + len(episode_value))

        return None

    @classmethod
    def _extract_episode_fallback(cls, file_name: str) -> Optional[str]:
        """
        anitopy 无法识别时的兜底集数提取（第xx話 / 第xx话 / 。01 等）
        """
        m = cls._FALLBACK_EPISODE_RE.search(file_name)
        if m:
            return m.group(1)
        m = cls._FALLBACK_PERIOD_RE.search(file_name)
        if m:
            return m.group(1)
        return None

    @staticmethod
    def _select_base_file(
        file_names: Iterable[str],
        ep_spans: Iterable[Tuple[int, int]],
    ) -> Tuple[List[str], List[Tuple[int, int]]]:
        """
        before_ep 多数投票选取基准文件，排除 OAD 等异类
        """
        before_groups: Dict[str, List[Tuple[str, Tuple[int, int]]]] = defaultdict(list)
        for name, span in zip(file_names, ep_spans):
            before_groups[name[: span[0]]].append((name, span))

        sorted_groups = sorted(before_groups.values(), key=len, reverse=True)
        majority_group = sorted_groups[0]

        majority_names: List[str] = []
        majority_spans: List[Tuple[int, int]] = []
        for name, span in majority_group:
            majority_names.append(name)
            majority_spans.append(span)
        return majority_names, majority_spans

    def _build_ep_only_template(
        self,
        file_names: List[str],
        ep_spans: List[Tuple[int, int]],
        use_majority: bool = True,
    ) -> str:
        """
        基于多数派文件生成仅含 {ep} 的模板
        """
        if use_majority:
            file_names, ep_spans = self._select_base_file(file_names, ep_spans)
        return self._build_ep_template_from_file(file_names[0], ep_spans[0])

    def _build_ep_template_from_file(
        self, file_name: str, ep_span: Tuple[int, int]
    ) -> str:
        start, end = ep_span
        return (
            self._escape_literal(file_name[:start])
            + "{ep}"
            + self._escape_literal(file_name[end:])
        )

    def _build_template_with_diff(
        self,
        file_names: List[str],
        ep_spans: List[Tuple[int, int]],
        use_majority: bool = True,
    ) -> Optional[str]:
        """
        多文件对比生成含 {a}/{b}/{c} 占位符的模板
        """
        if use_majority:
            file_names, ep_spans = self._select_base_file(file_names, ep_spans)
        if len(file_names) < 2:
            return None

        before_ep_set = {name[: span[0]] for name, span in zip(file_names, ep_spans)}
        if len(before_ep_set) != 1:
            return None

        after_ep_list = [name[span[1] :] for name, span in zip(file_names, ep_spans)]

        if len(set(after_ep_list)) == 1:
            return None

        template = self._build_ep_template_from_file(file_names[0], ep_spans[0])
        placeholders = ["a", "b", "c"]
        placeholder_idx = 0
        remaining_names = list(file_names)

        while remaining_names and placeholder_idx < len(placeholders):
            failed = self._find_unmatched(template, remaining_names)
            if not failed:
                break

            template = self._insert_variable_placeholder(
                template, failed, after_ep_list, file_names,
                placeholders[placeholder_idx]
            )
            placeholder_idx += 1

        if self._validate_auto_template(template, file_names):
            return template
        return None

    @staticmethod
    def _find_unmatched(
        template: str, file_names: List[str]
    ) -> List[str]:
        parser = FormatParser(eformat=template)
        failed: List[str] = []
        for name in file_names:
            if not parser.match(name):
                failed.append(name)
        return failed

    def _insert_variable_placeholder(
        self,
        template: str,
        failed_files: List[str],
        after_ep_list: List[str],
        all_file_names: List[str],
        placeholder: str,
    ) -> str:
        ep_marker = "{ep}"
        ep_pos = template.find(ep_marker)
        if ep_pos < 0:
            return template

        base_after_ep = after_ep_list[0]

        first_failed = ""
        for name, after_ep in zip(all_file_names, after_ep_list):
            if name in failed_files:
                first_failed = after_ep
                break

        if not first_failed:
            return template

        diff_start = self._first_diff_index(base_after_ep, first_failed)
        diff_end = self._last_diff_index(base_after_ep, first_failed)

        lcp_raw = base_after_ep[:diff_start]
        lcs_raw = base_after_ep[diff_end:] if diff_end < len(base_after_ep) else ""

        variable_candidates = [base_after_ep]
        for name, after_ep in zip(all_file_names, after_ep_list):
            if name in failed_files:
                variable_candidates.append(after_ep)

        variable_parts = []
        for ae in variable_candidates:
            end_pos = len(ae) - (len(base_after_ep) - diff_end)
            if end_pos > diff_start:
                variable_parts.append(ae[diff_start:end_pos])
            else:
                variable_parts.append("")

        if any(not part for part in variable_parts):
            while diff_start > 0 and any(not part for part in variable_parts):
                diff_start -= 1
                lcp_raw = lcp_raw[:-1]
                variable_parts = []
                for ae in variable_candidates:
                    end_pos = len(ae) - (len(base_after_ep) - diff_end)
                    if end_pos > diff_start:
                        variable_parts.append(ae[diff_start:end_pos])
                    else:
                        variable_parts.append("")

        before_ep = template[:ep_pos]
        lcp_escaped = self._escape_literal(lcp_raw)
        lcs_escaped = self._escape_literal(lcs_raw) if lcs_raw else ""

        return before_ep + ep_marker + lcp_escaped + f"{{{placeholder}}}" + lcs_escaped

    @staticmethod
    def _first_diff_index(a: str, b: str) -> int:
        min_len = min(len(a), len(b))
        for i in range(min_len):
            if a[i] != b[i]:
                return i
        return min_len

    @staticmethod
    def _last_diff_index(a: str, b: str) -> int:
        rev_a = a[::-1]
        rev_b = b[::-1]
        min_len = min(len(rev_a), len(rev_b))
        for i in range(min_len):
            if rev_a[i] != rev_b[i]:
                return len(a) - i
        return len(a) - min_len

    def _validate_auto_template(
        self,
        episode_format: str,
        file_names: List[str],
    ) -> bool:
        """
        用 FormatParser 校验自动生成的模板
        """
        if not episode_format:
            return False
        parser = FormatParser(eformat=episode_format)
        for name in file_names:
            if not parser.match(name):
                return False
            result = parser.split_episode(file_name=name, file_meta=None)
            if result[0] is None:
                return False
        return True

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
            if start == end:
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
