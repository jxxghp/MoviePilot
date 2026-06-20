import re
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Match, Optional, Tuple, Union

import anitopy

from app.core.config import settings
from app.core.metainfo import MetaInfoPath
from app.core.meta.metabase import MetaBase
from app.log import logger
from app.schemas import EpisodeFormatRule, FileItem


@dataclass(frozen=True)
class _TemplateParseResult:
    named: Dict[str, str]
    spans: Dict[str, Tuple[int, int]]


@lru_cache(maxsize=256)
def _compile_template_pattern(
    template: str,
    ep_group_name: Optional[str] = None,
):
    parts: List[str] = ["^"]
    cursor = 0
    while cursor < len(template):
        if template.startswith("{{", cursor):
            parts.append(re.escape("{"))
            cursor += 2
            continue
        if template.startswith("}}", cursor):
            parts.append(re.escape("}"))
            cursor += 2
            continue
        if template[cursor] == "{":
            end = template.find("}", cursor + 1)
            if end < 0:
                raise ValueError(f"模板存在未闭合占位符：{template}")
            group_name = template[cursor + 1:end]
            if not re.fullmatch(r"[A-Za-z_]\w*", group_name):
                raise ValueError(f"模板占位符名称无效：{template}")
            quantifier = ".+?" if group_name == ep_group_name else ".*?"
            parts.append(f"(?P<{group_name}>{quantifier})")
            cursor = end + 1
            continue
        if template[cursor] == "}":
            raise ValueError(f"模板存在未转义的右花括号：{template}")

        literal_end = cursor
        while literal_end < len(template) and template[literal_end] not in "{}":
            literal_end += 1
        parts.append(re.escape(template[cursor:literal_end]))
        cursor = literal_end
    parts.append("$")
    return re.compile("".join(parts))


def _match_template(
    template: str,
    text: str,
    ep_group_name: Optional[str] = None,
) -> Optional[_TemplateParseResult]:
    pattern = _compile_template_pattern(template, ep_group_name)
    result = pattern.match(text)
    if not result:
        return None
    group_names = result.groupdict()
    return _TemplateParseResult(
        named=group_names,
        spans={
            group_name: result.span(group_name)
            for group_name in group_names
        },
    )


class FormatParser(object):
    _key = ""
    _split_chars = r"\.|\s+|\(|\)|\[|]|-|\+|【|】|/|～|;|&|\||#|_|「|」|~"

    def __init__(self, eformat: str, details: Optional[str] = None, part: Optional[str] = None,
                 offset: Optional[str] = None, key: Optional[str] = "ep"):
        """
        :params eformat: 格式化字符串
        :params details: 格式化详情
        :params part: 分集
        :params offset: 偏移量 -10/EP*2
        :prams key: EP关键字
        """
        self._format = eformat
        self._start_ep = None
        self._end_ep = None
        if not offset:
            self.__offset = "EP"
        elif "EP" in offset:
            self.__offset = offset
        else:
            if offset.startswith("-") or offset.startswith("+"):
                self.__offset = f"EP{offset}"
            else:
                self.__offset = f"EP+{offset}"
        self._key = key
        self._part = None
        self._compiled_pattern = (
            _compile_template_pattern(self._format, self._key)
            if self._format
            else None
        )
        if part:
            self._part = part
        if details:
            if re.compile("\\d{1,4}-\\d{1,4}").match(details):
                self._start_ep = details
                self._end_ep = details
            else:
                tmp = details.split(",")
                if len(tmp) > 1:
                    self._start_ep = int(tmp[0])
                    self._end_ep = int(tmp[0]) if int(tmp[0]) > int(tmp[1]) else int(tmp[1])
                else:
                    self._start_ep = self._end_ep = int(tmp[0])

    @property
    def format(self):
        return self._format

    @property
    def start_ep(self):
        return self._start_ep

    @property
    def end_ep(self):
        return self._end_ep

    @property
    def part(self):
        return self._part

    @property
    def offset(self):
        return self.__offset

    def match(self, file: str) -> bool:
        if not self._format:
            return True
        s, e = self.__handle_single(file)
        if not s:
            return False
        if self._start_ep is None:
            return True
        if self._start_ep <= s <= self._end_ep:
            return True
        return False

    def split_episode(self, file_name: str, file_meta: MetaBase) -> Tuple[Optional[int], Optional[int], Optional[str]]:
        """
        拆分集数，返回开始集数，结束集数，Part信息
        """
        # 指定的具体集数，直接返回
        if self._start_ep is not None:
            if self._start_ep == self._end_ep:
                # `details` 格式为 `X-X` 或者 `X`
                if isinstance(self._start_ep, str):
                    # `details` 格式为 `X-X`
                    s, e = self._start_ep.split("-")
                    start_ep = self.__offset.replace("EP", s)
                    end_ep = self.__offset.replace("EP", e)
                    if int(s) == int(e):
                        return int(eval(start_ep)), None, self.part
                    return int(eval(start_ep)), int(eval(end_ep)), self.part
                else:
                    # `details` 格式为 `X`
                    start_ep = self.__offset.replace("EP", str(self._start_ep))
                    return int(eval(start_ep)), None, self.part
            elif not self._format:
                # `details` 格式为 `X,X`
                start_ep = self.__offset.replace("EP", str(self._start_ep))
                end_ep = self.__offset.replace("EP", str(self._end_ep))
                return int(eval(start_ep)), int(eval(end_ep)), self.part
        if not self._format:
            # 未填入`集数定位` 且没有`指定集数` 仅处理`集数偏移`
            start_ep = eval(self.__offset.replace("EP", str(file_meta.begin_episode))) if file_meta.begin_episode else None
            end_ep = eval(self.__offset.replace("EP", str(file_meta.end_episode))) if file_meta.end_episode else None
            return int(start_ep) if start_ep else None, int(end_ep) if end_ep else None, self.part
        else:
            # 有`集数定位`
            s, e = self.__handle_single(file_name)
            start_ep = self.__offset.replace("EP", str(s)) if s else None
            end_ep = self.__offset.replace("EP", str(e)) if e else None
            return int(eval(start_ep)) if start_ep else None, int(eval(end_ep)) if end_ep else None, self.part

    def __handle_single(self, file: str) -> Tuple[Optional[int], Optional[int]]:
        """
        处理单集，返回单集的开始和结束集数
        """
        if not self._format:
            return None, None
        ret = self._compiled_pattern.match(file) if self._compiled_pattern else None
        if not ret or self._key not in ret.groupdict():
            return None, None
        episodes = ret.group(self._key)
        if not re.compile(
            r"^([Ee][Pp]?)?(\d{1,4})(-([Ee][Pp]?)?(\d{1,4}))?$",
            re.IGNORECASE,
        ).match(episodes):
            return None, None
        episode_splits = list(filter(lambda x: re.compile(r'[a-zA-Z]*\d{1,4}', re.IGNORECASE).match(x),
                                     re.split(r'%s' % self._split_chars, episodes)))
        if len(episode_splits) == 1:
            return int(re.compile(r'[a-zA-Z]*', re.IGNORECASE).sub("", episode_splits[0])), None
        else:
            return int(re.compile(r'[a-zA-Z]*', re.IGNORECASE).sub("", episode_splits[0])), int(
                re.compile(r'[a-zA-Z]*', re.IGNORECASE).sub("", episode_splits[1]))


@dataclass(frozen=True)
class _AutoRecommendSample:
    file_name: str
    ep_span: Tuple[int, int]
    expected_episode: str
    source_kind: str = "media"
    native_episode: Optional[str] = None
    native_verified: bool = False
    used_native_fallback: bool = False


class EpisodeFormatRuleHelper:
    """
    集数定位规则辅助类
    """

    _MIN_MEDIA_FILE_SIZE_BYTES = 100 * 1024 * 1024
    _MIN_AUTO_VALID_MEDIA_COVERAGE = 0.6
    _EMPTY_META = MetaBase(title="")

    _EP_RANGE_RE = re.compile(
        r"(?<![A-Za-z0-9])[Ee][Pp]?(\d{1,4}(?:-[Ee]?[Pp]?\d{1,4})+)(?!\d)"
    )
    _EP_PREFIX_RE = re.compile(r"(?<![A-Za-z0-9])[Ee][Pp]?(\d{1,4})(?!\d)")
    _SEASON_EP_RANGE_RE = re.compile(
        r"[Ss]\d{1,4}[Ee][Pp]?(\d{1,4}(?:-[Ee]?[Pp]?\d{1,4})+)(?!\d)"
    )
    _SEASON_EP_RE = re.compile(r"[Ss]\d{1,4}[Ee][Pp]?(\d{1,4})(?!\d)")
    _HASH_EP_RE = re.compile(r"(?<!\d)#(\d{1,4})(?!\d)")
    _BRACKET_EP_RE = re.compile(r"[\[【](\d{1,4})[\]】]")
    _FALLBACK_BRACKET_EP_RE = re.compile(r"[\[【](\d{1,3})[\]】]")
    _FALLBACK_EPISODE_RE = re.compile(r"第(\d{1,4})[話话]")
    _FALLBACK_EPISODE_JI_RE = re.compile(r"第(\d{1,4})集")
    _FALLBACK_PERIOD_RE = re.compile(r"。(\d{1,4})\s")
    _CJK_EP_RE = re.compile(r"第(\d{1,4})(?:[話话集])")
    _SPECIAL_SAMPLE_RE = re.compile(
        r"\[(?:"
        r"SP\d+"
        r"|NC(?:OP|ED)(?:[_\s-]*EP\d+)?(?:\s+VER\.\d+)?"
        r"|OP"
        r"|ED"
        r"|MENU(?:\d+|OVA)?"
        r"|OVA(?:\s+TRAILER)?"
        r"|OAD"
        r"|PV\d*"
        r"|CM(?:\d+| COLLECTION)?"
        r"|TRAILER"
        r"|WEB PREVIEW(?:\s+\d+)?"
        r"|SERIES REVIEW"
        r"|TABLE GAME"
        r"|TV SPOTS?"
        r"|\d+\((?:OVA|OAD|SP)\d+\)"
        r")\]",
        re.IGNORECASE,
    )

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
            compatibility_samples = self._build_detected_samples(
                self._filter_by_extension_and_size(sample_files),
            )
            if compatibility_samples and not self._validate_auto_template(
                episode_format,
                compatibility_samples,
            ):
                logger.warn(f"集数定位规则 {rule.name} 附加文件兼容性校验失败")
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
        if not sample_files:
            return False, "目录中没有可用于识别的媒体文件", None

        candidates = self._filter_by_extension_and_size(sample_files)
        size_filter_relaxed = False
        if not candidates:
            candidates = self._filter_by_extension_and_size(
                sample_files, ignore_size=True
            )
            size_filter_relaxed = bool(candidates)
        if not candidates:
            return False, "无匹配自定义定位规则，智能生成失败", None

        valid_samples = self._build_detected_samples(candidates)
        native_verified_count = 0
        native_fallback_count = 0
        native_conflict_count = 0
        episode_not_detected_count = 0
        for item in valid_samples:
            if item.native_verified:
                native_verified_count += 1
            if item.used_native_fallback:
                native_fallback_count += 1
        for item in sorted(
            candidates,
            key=lambda entry: (
                self._sample_kind_priority(self._get_file_kind(entry)),
                (entry.name or ""),
                (entry.path or ""),
            ),
        ):
            file_name = item.name or ""
            if self._is_special_sample(file_name):
                continue
            normalized_episode, native_episode, used_native_fallback, native_verified = (
                self._extract_episode_with_native_fallback(item)
            )
            if normalized_episode and native_episode and not (
                used_native_fallback or native_verified
            ):
                native_conflict_count += 1
                logger.warn(
                    "自动推荐样本与原生集数识别冲突，跳过："
                    f"{file_name} - auto={normalized_episode}, native={native_episode}"
                )
                continue
            expected_start, _ = self._parse_episode_value(normalized_episode)
            if expected_start is None:
                episode_not_detected_count += 1
                continue
            if expected_start <= 0:
                continue
            if self._locate_episode(file_name, normalized_episode) is None:
                episode_not_detected_count += 1

        if not valid_samples:
            if native_conflict_count:
                return (
                    False,
                    "样本命名与原生识别结果冲突，建议补充集数定位规则",
                    None,
                )
            if episode_not_detected_count:
                return False, "样本未识别到有效集数，智能生成失败", None
            return False, "无匹配自定义定位规则，智能生成失败", None

        if native_conflict_count and len(valid_samples) < len(candidates):
            return (
                False,
                "样本命名与原生识别结果冲突，建议补充集数定位规则",
                None,
            )

        candidate_media_count = 0
        for item in candidates:
            if (
                    self._get_file_kind(item) == "media"
                    and not self._is_special_sample(item.name or "")
            ):
                candidate_media_count += 1

        valid_media_count = 0
        for item in valid_samples:
            if item.source_kind == "media":
                valid_media_count += 1

        if (
                candidate_media_count > 1
                and valid_media_count / candidate_media_count
                < self._MIN_AUTO_VALID_MEDIA_COVERAGE
        ):
            logger.warn(
                "有效正片样本覆盖率不足，放弃智能生成："
                f"valid_media={valid_media_count}, candidate_media={candidate_media_count}"
            )
            return False, "有效正片样本覆盖率不足，建议补充集数定位规则", None

        majority_samples, clear_majority = self._select_base_samples(valid_samples)
        logger.debug(
            "自动推荐多数派样本："
            f"valid={len(valid_samples)}, majority={len(majority_samples)}, "
            f"clear_majority={clear_majority}, files="
            f"{[(sample.file_name, sample.expected_episode, sample.ep_span) for sample in majority_samples]}"
        )
        if len(valid_samples) > 1 and not clear_majority:
            logger.warn("自动生成样本未形成明确多数派，放弃推荐")
            return False, "样本命名差异过大，建议补充集数定位规则", None

        majority_names = [sample.file_name for sample in majority_samples]
        majority_spans = [sample.ep_span for sample in majority_samples]

        episode_format = self._build_ep_only_template(
            majority_names, majority_spans, use_majority=False
        )
        logger.debug(
            "自动推荐基础模板："
            f"sample={majority_names[0] if majority_names else None}, "
            f"span={majority_spans[0] if majority_spans else None}, template={episode_format}"
        )
        if not self._validate_auto_template(episode_format, majority_samples):
            diff_result = self._build_template_with_diff(
                majority_names, majority_spans, use_majority=False
            )
            logger.debug(
                "自动推荐差异模板尝试："
                f"base={episode_format}, diff={diff_result}"
            )
            if diff_result and self._validate_auto_template(
                diff_result, majority_samples
            ):
                episode_format = diff_result
            else:
                logger.warn("多文件对比未通过模板校验，自动生成失败")
                return False, "无匹配自定义定位规则，智能生成失败", None

        sample_file = majority_names[0]
        low_confidence = len(majority_samples) == 1 or size_filter_relaxed
        reasons = self._build_auto_reasons(
            sample_count=len(valid_samples),
            majority_count=len(majority_samples),
            size_filter_relaxed=size_filter_relaxed,
            native_fallback_count=native_fallback_count,
            native_verified_count=native_verified_count,
        )
        logger.info(f"智能分析生成集数定位模板：{sample_file} -> {episode_format}")

        return True, "", {
            "rule_name": "智能分析",
            "episode_format": episode_format,
            "sample_file": sample_file,
            "pattern": None,
            "sample_count": len(valid_samples),
            "majority_count": len(majority_samples),
            "confidence": "low" if low_confidence else "high",
            "size_filter_relaxed": size_filter_relaxed,
            "native_verified_count": native_verified_count,
            "native_fallback_count": native_fallback_count,
            "native_conflict_count": native_conflict_count,
            "reason": reasons[0] if reasons else None,
            "reasons": reasons,
            "message": self._build_auto_message(
                sample_count=len(valid_samples),
                majority_count=len(majority_samples),
                size_filter_relaxed=size_filter_relaxed,
                native_fallback_count=native_fallback_count,
            ),
        }

    @staticmethod
    def _build_auto_message(
        sample_count: int,
        majority_count: int,
        size_filter_relaxed: bool,
        native_fallback_count: int,
    ) -> str:
        if majority_count <= 1:
            return "样本不足，仅基于单文件智能生成（仅供参考）"
        if size_filter_relaxed:
            return "已放宽体积限制智能生成模板（仅供参考）"
        if native_fallback_count:
            return "已结合原生集数识别智能生成模板（仅供参考）"
        if sample_count != majority_count:
            return "已根据多数派样本智能生成模板（仅供参考）"
        return "无匹配自定义定位规则，已智能生成（仅供参考）"

    @staticmethod
    def _build_auto_reasons(
        sample_count: int,
        majority_count: int,
        size_filter_relaxed: bool,
        native_fallback_count: int,
        native_verified_count: int,
    ) -> List[str]:
        reasons: List[str] = []
        if majority_count <= 1:
            reasons.append("single_sample_only")
        if size_filter_relaxed:
            reasons.append("small_files_fallback")
        if native_fallback_count:
            reasons.append("native_meta_fallback")
        elif native_verified_count:
            reasons.append("native_meta_verified")
        if sample_count != majority_count:
            reasons.append("majority_samples_only")
        if not reasons:
            reasons.append("auto_recommendation")
        return reasons

    @staticmethod
    def _filter_by_extension_and_size(
        files: List[FileItem],
        ignore_size: bool = False,
    ) -> List[FileItem]:
        """
        第一轮筛选：主视频扩展名白名单 + 体积门槛，字幕/外挂音频始终允许参与
        """
        candidates: List[FileItem] = []
        for item in files:
            file_kind = EpisodeFormatRuleHelper._get_file_kind(item)
            if file_kind == "other":
                continue
            if (
                file_kind == "media"
                and not ignore_size
                and (item.size or 0) < EpisodeFormatRuleHelper._MIN_MEDIA_FILE_SIZE_BYTES
            ):
                continue
            candidates.append(item)
        return candidates

    @staticmethod
    def _get_file_kind(item: FileItem) -> str:
        extension = f".{(item.extension or '').lower().lstrip('.')}" if item.extension else ""
        if extension in settings.RMT_MEDIAEXT:
            return "media"
        if extension in settings.RMT_SUBEXT:
            return "subtitle"
        if extension in settings.RMT_AUDIOEXT:
            return "audio"
        return "other"

    @staticmethod
    def _sample_kind_priority(kind: str) -> int:
        return {
            "media": 0,
            "subtitle": 1,
            "audio": 2,
        }.get(kind, 9)

    @classmethod
    def _is_special_sample(cls, file_name: str) -> bool:
        return bool(cls._SPECIAL_SAMPLE_RE.search(file_name or ""))

    def _build_detected_samples(
        self,
        candidates: List[FileItem],
    ) -> List[_AutoRecommendSample]:
        valid_samples: List[_AutoRecommendSample] = []
        for item in sorted(
            candidates,
            key=lambda entry: (
                self._sample_kind_priority(self._get_file_kind(entry)),
                (entry.name or ""),
                (entry.path or ""),
            ),
        ):
            file_name = item.name or ""
            if self._is_special_sample(file_name):
                # SP/NCOP/NCED/OP/ED/MENU 等明显特典样本不参与正片模板自动推荐。
                continue
            normalized_episode, native_episode, used_native_fallback, native_verified = (
                self._extract_episode_with_native_fallback(item)
            )
            if normalized_episode and native_episode and not (
                used_native_fallback or native_verified
            ):
                continue
            expected_start, _ = self._parse_episode_value(normalized_episode)
            if expected_start is None:
                continue
            if expected_start <= 0:
                # 00 集通常归属于特殊季，不参与正片模板自动推荐。
                continue
            if normalized_episode and not normalized_episode.isdigit():
                # 非纯整数的特殊集数当前不在 FormatParser 消费契约内，
                # 继续参与推荐只会把正片模板生成带偏。
                continue

            ep_span = self._locate_episode(file_name, normalized_episode)
            if ep_span is None:
                logger.debug(
                    "自动推荐样本跳过：未定位到集数 token - "
                    f"{file_name} - episode={normalized_episode}"
                )
                continue

            logger.debug(
                "自动推荐样本入选："
                f"{file_name} - episode={normalized_episode}, span={ep_span}, "
                f"matched={file_name[ep_span[0]:ep_span[1]]}, "
                f"kind={self._get_file_kind(item)}"
            )
            valid_samples.append(
                _AutoRecommendSample(
                    file_name=file_name,
                    ep_span=ep_span,
                    expected_episode=normalized_episode,
                    source_kind=self._get_file_kind(item),
                    native_episode=native_episode,
                    native_verified=native_verified,
                    used_native_fallback=used_native_fallback,
                )
            )
        return valid_samples

    @classmethod
    def _locate_episode(
        cls,
        file_name: str,
        episode_value: str,
    ) -> Optional[Tuple[int, int]]:
        """
        三级策略反向定位 episode_number 在文件名中的位置
        """
        normalized_episode_value = cls._normalize_episode_value(episode_value)
        for matcher in (
            cls._EP_RANGE_RE,
            cls._EP_PREFIX_RE,
            cls._SEASON_EP_RANGE_RE,
            cls._SEASON_EP_RE,
            cls._HASH_EP_RE,
            cls._BRACKET_EP_RE,
            cls._CJK_EP_RE,
        ):
            for match in matcher.finditer(file_name):
                if cls._episode_value_equals(
                    match.group(1),
                    normalized_episode_value,
                ):
                    return match.span(1)

        for candidate in cls._build_episode_candidates(normalized_episode_value):
            token_pattern = re.compile(
                rf"(?:(?<=^)|(?<=[\s._\-\[\]【】()「」『』《》〈〉〔〕]))"
                rf"{re.escape(candidate)}"
                rf"(?:(?=$)|(?=[\s._\-\[\]【】()「」『』《》〈〉〔〕]))"
            )
            matches = list(token_pattern.finditer(file_name))
            if matches:
                return matches[-1].span()
        return None

    @staticmethod
    def _normalize_episode_value(episode_value) -> str:
        if isinstance(episode_value, list):
            parts = [str(part) for part in episode_value]
        else:
            parts = str(episode_value).split("-")
        normalized_parts = [
            re.sub(r"^[Ee][Pp]?", "", part.strip())
            for part in parts
            if str(part).strip()
        ]
        return "-".join(normalized_parts)

    @staticmethod
    def _parse_episode_value(
        expected_episode: Optional[str],
    ) -> Tuple[Optional[int], Optional[int]]:
        if not expected_episode:
            return None, None
        parts = []
        for part in str(expected_episode).split("-"):
            cleaned = re.sub(r"^[Ee][Pp]?", "", part.strip())
            number_match = re.search(r"\d{1,4}", cleaned)
            if not number_match:
                return None, None
            parts.append(int(number_match.group()))
        if not parts:
            return None, None
        if len(parts) == 1 or parts[-1] == parts[0]:
            return parts[0], None
        return parts[0], parts[-1]

    @classmethod
    def _episode_value_equals(
        cls,
        actual_episode: Optional[str],
        expected_episode: Optional[str],
    ) -> bool:
        if not actual_episode or not expected_episode:
            return False
        return cls._parse_episode_value(actual_episode) == cls._parse_episode_value(
            expected_episode
        )

    @classmethod
    def _build_episode_candidates(
        cls,
        episode_value: Optional[str],
    ) -> List[str]:
        start_episode, end_episode = cls._parse_episode_value(episode_value)
        if start_episode is None:
            return []
        candidates: List[str] = []
        if end_episode is None:
            for width in range(1, 5):
                candidates.append(str(start_episode).zfill(width))
        else:
            for width in range(1, 5):
                start_text = str(start_episode).zfill(width)
                end_text = str(end_episode).zfill(width)
                candidates.append(f"{start_text}-{end_text}")
                candidates.append(f"{start_text}-E{end_text}")
                candidates.append(f"{start_text}-EP{end_text}")
        # 保证顺序稳定，同时去重
        return list(dict.fromkeys(candidates))

    @classmethod
    def _extract_native_episode(cls, item: FileItem) -> Optional[str]:
        source_path = item.path or item.name
        if not source_path:
            return None
        try:
            meta = MetaInfoPath(Path(source_path))
        except Exception as err:
            logger.warn(f"原生集数识别失败：{source_path} - {err}")
            return None
        if meta.begin_episode is None:
            return None
        if meta.end_episode is not None and meta.end_episode != meta.begin_episode:
            return f"{meta.begin_episode}-{meta.end_episode}"
        return str(meta.begin_episode)

    @classmethod
    def _should_degrade_native_conflict(
        cls,
        file_name: str,
        normalized_episode: Optional[str],
        native_episode: Optional[str],
    ) -> bool:
        """
        判断原生集数冲突是否应降级处理。

        当自动定位到的集数 token 明确出现在文件名后部，而原生识别出来的数字
        只出现在更靠前的位置时，通常是标题续作号或目录序号误判，不应继续作
        为自动推荐的否决条件。
        """
        if not file_name or not normalized_episode or not native_episode:
            return False

        auto_span = cls._locate_episode(file_name, normalized_episode)
        native_span = cls._locate_episode(file_name, native_episode)
        if not auto_span or not native_span:
            return False
        return native_span[1] <= auto_span[0]

    @classmethod
    def _should_prefer_fallback_episode(
        cls,
        file_name: str,
        anitopy_episode: Optional[Union[str, List[str]]],
        fallback_episode: Optional[Union[str, List[str]]],
    ) -> bool:
        """
        当 anitopy 命中了标题前部数字，而 fallback 命中了更靠后的显式集数 token 时，
        优先使用 fallback 结果。
        """
        if not file_name or not anitopy_episode or not fallback_episode:
            return False
        normalized_anitopy_episode = cls._normalize_episode_value(anitopy_episode)
        normalized_fallback_episode = cls._normalize_episode_value(fallback_episode)
        if cls._episode_value_equals(
            normalized_anitopy_episode,
            normalized_fallback_episode,
        ):
            return False
        _, anitopy_end_episode = cls._parse_episode_value(normalized_anitopy_episode)
        if anitopy_end_episode is not None:
            return False

        anitopy_span = cls._locate_episode(file_name, normalized_anitopy_episode)
        fallback_span = cls._locate_episode(file_name, normalized_fallback_episode)
        if not anitopy_span or not fallback_span:
            return False
        return anitopy_span[1] <= fallback_span[0]

    def _extract_episode_with_native_fallback(
        self,
        item: FileItem,
    ) -> Tuple[Optional[str], Optional[str], bool, bool]:
        file_name = item.name or ""
        native_episode = self._extract_native_episode(item)
        episode_number = None
        anitopy_episode = None
        try:
            result = anitopy.parse(file_name)
            episode_number = result.get("episode_number")
            anitopy_episode = episode_number
        except Exception as err:
            logger.warn(f"anitopy 解析失败：{file_name} - {err}")
        fallback_episode = self._extract_episode_fallback(file_name)
        if not episode_number:
            episode_number = fallback_episode
        elif self._should_prefer_fallback_episode(
            file_name,
            anitopy_episode,
            fallback_episode,
        ):
            episode_number = fallback_episode
        normalized_episode = (
            self._normalize_episode_value(episode_number)
            if episode_number
            else None
        )
        logger.debug(
            "自动推荐集数提取："
            f"{file_name} - anitopy={anitopy_episode}, "
            f"fallback={fallback_episode}, normalized={normalized_episode}, "
            f"native={native_episode}"
        )
        used_native_fallback = False
        native_verified = False
        if normalized_episode and native_episode:
            if self._episode_value_equals(normalized_episode, native_episode):
                native_verified = True
            elif self._should_degrade_native_conflict(
                file_name,
                normalized_episode,
                native_episode,
            ):
                logger.info(
                    "原生集数识别疑似命中标题序号，降级冲突权重："
                    f"{file_name} - auto={normalized_episode}, native={native_episode}"
                )
                native_episode = None
            else:
                return normalized_episode, native_episode, False, False
        elif not normalized_episode and native_episode:
            normalized_episode = native_episode
            used_native_fallback = True
        return normalized_episode, native_episode, used_native_fallback, native_verified

    @classmethod
    def _extract_episode_fallback(cls, file_name: str) -> Optional[str]:
        """
        anitopy 无法识别时的兜底集数提取。

        优先尝试结构更明确的季集/井号/方括号集数，再退回到中日韩常见文案。
        """
        match = cls._SEASON_EP_RANGE_RE.search(file_name)
        if match:
            return match.group(1)
        match = cls._SEASON_EP_RE.search(file_name)
        if match:
            return match.group(1)
        hash_matches = list(cls._HASH_EP_RE.finditer(file_name))
        if hash_matches:
            return hash_matches[-1].group(1)
        bracket_matches = list(cls._FALLBACK_BRACKET_EP_RE.finditer(file_name))
        if bracket_matches:
            return bracket_matches[-1].group(1)
        match = cls._FALLBACK_EPISODE_RE.search(file_name)
        if match:
            return match.group(1)
        match = cls._FALLBACK_EPISODE_JI_RE.search(file_name)
        if match:
            return match.group(1)
        match = cls._FALLBACK_PERIOD_RE.search(file_name)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _select_base_samples(
        samples: Iterable[_AutoRecommendSample],
    ) -> Tuple[List[_AutoRecommendSample], bool]:
        """
        before_ep 多数投票选取基准文件，排除 OAD 等异类
        """
        before_groups: Dict[str, List[_AutoRecommendSample]] = defaultdict(list)
        for sample in samples:
            before_groups[sample.file_name[: sample.ep_span[0]]].append(sample)

        sorted_groups = sorted(
            before_groups.items(),
            key=lambda item: (-len(item[1]), item[0]),
        )
        majority_group = sorted(
            sorted_groups[0][1],
            key=lambda item: (
                EpisodeFormatRuleHelper._sample_kind_priority(item.source_kind),
                item.file_name,
                item.ep_span[0],
                item.ep_span[1],
            ),
        )
        clear_majority = (
            len(sorted_groups) == 1
            or len(majority_group) > len(sorted_groups[1][1])
        )
        return majority_group, clear_majority

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
            majority_samples, _ = self._select_base_samples(
                _AutoRecommendSample(
                    file_name=name,
                    ep_span=span,
                    expected_episode="",
                )
                for name, span in zip(file_names, ep_spans)
            )
            file_names = [sample.file_name for sample in majority_samples]
            ep_spans = [sample.ep_span for sample in majority_samples]
        return self._build_ep_template_from_file(file_names[0], ep_spans[0])

    def _build_ep_template_from_file(
        self,
        file_name: str,
        ep_span: Tuple[int, int],
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
            majority_samples, _ = self._select_base_samples(
                _AutoRecommendSample(
                    file_name=name,
                    ep_span=span,
                    expected_episode="",
                )
                for name, span in zip(file_names, ep_spans)
            )
            file_names = [sample.file_name for sample in majority_samples]
            ep_spans = [sample.ep_span for sample in majority_samples]
        if len(file_names) < 2:
            return None

        before_ep_set = {name[: span[0]] for name, span in zip(file_names, ep_spans)}
        if len(before_ep_set) != 1:
            return None

        after_ep_list = [name[span[1]:] for name, span in zip(file_names, ep_spans)]
        if len(set(after_ep_list)) == 1:
            return None

        template = self._build_ep_template_from_file(file_names[0], ep_spans[0])
        placeholders = ["a", "b", "c"]
        placeholder_idx = 0

        while placeholder_idx < len(placeholders):
            failed = self._find_unmatched(template, file_names)
            if not failed:
                break
            updated_template = self._insert_variable_placeholder(
                template,
                failed,
                after_ep_list,
                file_names,
                placeholders[placeholder_idx],
            )
            if updated_template == template:
                break
            template = updated_template
            placeholder_idx += 1
        return template

    @staticmethod
    def _find_unmatched(
        template: str,
        file_names: List[str],
    ) -> List[str]:
        parser = EpisodeFormatRuleHelper._create_format_parser(
            template,
            context="多文件对比预校验",
        )
        if not parser:
            return list(file_names)
        failed: List[str] = []
        for name in file_names:
            if not EpisodeFormatRuleHelper._safe_match_template(
                parser,
                name,
                context="多文件对比预校验",
            ):
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

        current_after_ep_template = template[ep_pos + len(ep_marker):]
        base_after_ep = after_ep_list[0]
        existing_spans = self._collect_placeholder_spans(
            current_after_ep_template, base_after_ep
        )
        failed_after_ep_list = [
            after_ep
            for name, after_ep in zip(all_file_names, after_ep_list)
            if name in failed_files
        ]
        next_span = self._find_next_variable_span(
            base_after_ep,
            failed_after_ep_list,
            existing_spans,
        )
        if next_span is None:
            return template

        updated_spans = existing_spans + [
            (next_span[0], next_span[1], placeholder)
        ]
        before_ep = template[:ep_pos]
        return before_ep + ep_marker + self._render_after_ep_template(
            base_after_ep,
            updated_spans,
        )

    @staticmethod
    def _collect_placeholder_spans(
        after_ep_template: str,
        base_after_ep: str,
    ) -> List[Tuple[int, int, str]]:
        if not after_ep_template or "{" not in after_ep_template:
            return []
        result = EpisodeFormatRuleHelper._safe_parse_template(
            after_ep_template,
            base_after_ep,
            context="占位符区间收集",
        )
        if not result:
            return []
        spans: List[Tuple[int, int, str]] = []
        for name, span in result.spans.items():
            spans.append((span[0], span[1], name))
        spans.sort(key=lambda item: item[0])
        return spans

    def _find_next_variable_span(
        self,
        base_after_ep: str,
        failed_after_ep_list: List[str],
        existing_spans: List[Tuple[int, int, str]],
    ) -> Optional[Tuple[int, int]]:
        cursor = 0
        literal_gaps: List[Tuple[int, int]] = []
        for start, end, _ in existing_spans:
            if cursor < start:
                literal_gaps.append((cursor, start))
            cursor = end
        if cursor < len(base_after_ep):
            literal_gaps.append((cursor, len(base_after_ep)))

        for gap_start, gap_end in literal_gaps:
            if gap_start >= gap_end:
                continue
            probe_template = self._render_after_ep_template(
                base_after_ep,
                existing_spans + [(gap_start, gap_end, "probe")],
            )
            probe_values: List[str] = []
            base_gap = base_after_ep[gap_start:gap_end]
            for failed_after_ep in failed_after_ep_list:
                result = self._safe_parse_template(
                    probe_template,
                    failed_after_ep,
                    context="变量区间探测",
                )
                if not result:
                    continue
                probe_value = result.named.get("probe")
                if probe_value is None or probe_value == base_gap:
                    continue
                probe_values.append(probe_value)
            if not probe_values:
                continue

            relative_span = self._calculate_variable_span(base_gap, probe_values)
            if relative_span is None:
                continue
            return gap_start + relative_span[0], gap_start + relative_span[1]
        return None

    def _calculate_variable_span(
        self,
        base_text: str,
        compare_texts: List[str],
    ) -> Optional[Tuple[int, int]]:
        candidates = [base_text] + compare_texts
        prefix_len = self._common_prefix_length(candidates)
        suffix_len = self._common_suffix_length(candidates, prefix_len)
        end_pos = len(base_text) - suffix_len
        if prefix_len >= end_pos:
            base_part = base_text[prefix_len:end_pos]
            compare_parts = [
                text[
                    prefix_len:
                    len(text) - suffix_len if suffix_len else len(text)
                ]
                for text in compare_texts
            ]
            if not base_part and any(compare_parts):
                return prefix_len, prefix_len
            return None

        base_part = base_text[prefix_len:end_pos]
        compare_parts = [
            text[
                prefix_len:
                len(text) - suffix_len if suffix_len else len(text)
            ]
            for text in compare_texts
        ]
        if any(not part for part in [base_part] + compare_parts):
            if not base_part and any(compare_parts):
                return prefix_len, prefix_len
            if base_part and any(part == "" for part in compare_parts):
                return prefix_len, end_pos
            return None
        return prefix_len, end_pos

    @staticmethod
    def _common_prefix_length(texts: List[str]) -> int:
        if not texts:
            return 0
        min_len = min(len(text) for text in texts)
        prefix_len = 0
        while prefix_len < min_len:
            current_char = texts[0][prefix_len]
            if any(text[prefix_len] != current_char for text in texts[1:]):
                break
            prefix_len += 1
        return prefix_len

    @staticmethod
    def _common_suffix_length(
        texts: List[str],
        prefix_len: int = 0,
    ) -> int:
        if not texts:
            return 0
        suffix_len = 0
        min_len = min(len(text) for text in texts)
        while suffix_len < min_len - prefix_len:
            current_char = texts[0][-suffix_len - 1]
            if any(text[-suffix_len - 1] != current_char for text in texts[1:]):
                break
            suffix_len += 1
        return suffix_len

    def _render_after_ep_template(
        self,
        base_after_ep: str,
        spans: List[Tuple[int, int, str]],
    ) -> str:
        template_parts: List[str] = []
        cursor = 0
        for start, end, name in sorted(spans, key=lambda item: item[0]):
            if start < cursor or end < start:
                continue
            template_parts.append(
                self._escape_literal(base_after_ep[cursor:start])
            )
            template_parts.append(f"{{{name}}}")
            cursor = end
        template_parts.append(self._escape_literal(base_after_ep[cursor:]))
        return "".join(template_parts)

    def _validate_auto_template(
        self,
        episode_format: str,
        samples: List[_AutoRecommendSample],
    ) -> bool:
        """
        用 FormatParser 校验自动生成的模板
        """
        if not episode_format:
            return False
        parser = self._create_format_parser(
            episode_format,
            context="自动模板校验",
        )
        if not parser:
            return False
        for sample in samples:
            if not self._safe_match_template(
                parser,
                sample.file_name,
                context="自动模板校验",
            ):
                logger.debug(
                    "自动模板校验失败：模板未命中文件 - "
                    f"template={episode_format}, file={sample.file_name}"
                )
                return False
            start_episode, end_episode, _ = self._safe_split_episode(
                parser,
                sample.file_name,
                context="自动模板校验",
            )
            if not self._episode_matches(
                start_episode,
                end_episode,
                sample.expected_episode,
            ):
                logger.debug(
                    "自动模板校验失败：集数不匹配 - "
                    f"template={episode_format}, file={sample.file_name}, "
                    f"expected={sample.expected_episode}, actual={start_episode}-{end_episode}"
                )
                return False
            if sample.native_episode and not self._episode_matches(
                start_episode,
                end_episode,
                sample.native_episode,
            ):
                logger.debug(
                    "自动模板校验失败：与原生集数不一致 - "
                    f"template={episode_format}, file={sample.file_name}, "
                    f"native={sample.native_episode}, actual={start_episode}-{end_episode}"
                )
                return False
        return True

    @staticmethod
    def _match_rule(
        rule: EpisodeFormatRule,
        sample_files: List[FileItem],
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
            if (
                rule.min_file_size_mb
                and EpisodeFormatRuleHelper._get_file_kind(item) == "media"
                and (item.size or 0) < rule.min_file_size_mb * 1024 * 1024
            ):
                continue
            match_result = compiled_pattern.search(item.name or "")
            if not match_result or "ep" not in match_result.groupdict():
                continue
            matched_samples.append((item, match_result))
        return matched_samples

    def _build_template(
        self,
        file_name: str,
        match_result: Match[str],
    ) -> Optional[str]:
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

        if not group_items or not any(
            group_name == "ep"
            for _, _, group_name in group_items
        ):
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
        parser = self._create_format_parser(
            episode_format,
            context="规则模板校验",
        )
        if not parser:
            return False
        for item, match_result in matched_samples:
            file_name = item.name or ""
            if not self._safe_match_template(
                parser,
                file_name,
                context="规则模板校验",
            ):
                return False
            start_episode, end_episode, _ = self._safe_split_episode(
                parser,
                file_name,
                context="规则模板校验",
            )
            expected_episode = match_result.groupdict().get("ep")
            if not self._episode_matches(
                start_episode,
                end_episode,
                expected_episode,
            ):
                return False
        return True

    @staticmethod
    def _create_format_parser(
        episode_format: str,
        context: str,
    ) -> Optional[FormatParser]:
        try:
            return FormatParser(eformat=episode_format)
        except Exception as err:
            logger.warn(f"{context} 创建模板解析器失败：{episode_format} - {err}")
            return None

    @staticmethod
    def _safe_match_template(
        parser: FormatParser,
        file_name: str,
        context: str,
    ) -> bool:
        try:
            return parser.match(file_name)
        except Exception as err:
            logger.warn(f"{context} 模板匹配失败：{file_name} - {err}")
            return False

    @classmethod
    def _safe_split_episode(
        cls,
        parser: FormatParser,
        file_name: str,
        context: str,
    ) -> Tuple[Optional[int], Optional[int], Optional[str]]:
        try:
            return parser.split_episode(
                file_name=file_name,
                file_meta=cls._EMPTY_META,
            )
        except Exception as err:
            logger.warn(f"{context} 集数拆分失败：{file_name} - {err}")
            return None, None, None

    @staticmethod
    def _safe_parse_template(
        template: str,
        file_name: str,
        context: str,
    ) -> Optional[_TemplateParseResult]:
        try:
            return _match_template(template, file_name)
        except Exception as err:
            logger.warn(f"{context} parse 模板解析失败：{template} <- {file_name} - {err}")
            return None

    @classmethod
    def _episode_matches(
        cls,
        actual_start: Optional[int],
        actual_end: Optional[int],
        expected_episode: Optional[str],
    ) -> bool:
        """
        校验模板提取出的集数是否与期望值一致
        """
        expected_start, expected_end = cls._parse_episode_value(expected_episode)
        if actual_start is None or expected_start is None:
            return False
        if actual_start != expected_start:
            return False
        if expected_end is None:
            return actual_end is None
        return actual_end == expected_end

    @staticmethod
    def _normalize_pattern(pattern: str) -> str:
        """
        将 PCRE 风格命名组转为 Python re 可识别的语法
        """
        return re.sub(
            r"\(\?<([a-zA-Z_][a-zA-Z0-9_]*)>",
            r"(?P<\1>",
            pattern,
        )

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
