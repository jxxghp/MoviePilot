import shutil
from pathlib import Path
from typing import Union

import ruamel.yaml
from ruamel.yaml import CommentedMap

from app.platform.config import settings
from app.platform.log import logger
from app.schemas.category import (
    CategoryConditionDecision,
    CategoryConfig,
    CategoryRouteDecision,
    CategoryRuleDecision,
    RouteDiagnosticWarning,
)
from app.foundation.singleton import WeakSingleton

HEADER_COMMENTS = """####### 配置说明 #######
# 1. 该配置文件用于配置电影和电视剧的分类策略，配置后程序会按照配置的分类策略名称进行分类，配置文件采用yaml格式，需要严格符合语法规则
# 2. 配置文件中的一级分类名称：`movie`、`tv` 为固定名称不可修改，二级名称同时也是目录名称，会按先后顺序匹配，匹配后程序会按这个名称建立二级目录
# 3. 支持的分类条件：
#   `original_language` 语种，具体含义参考下方字典
#   `production_countries` 国家或地区（电影）、`origin_country` 国家或地区（电视剧），具体含义参考下方字典
#   `genre_ids` 内容类型，具体含义参考下方字典
#   `release_year` 发行年份，格式：YYYY，电影实际对应`release_date`字段，电视剧实际对应`first_air_date`字段，支持范围设定，如：`YYYY-YYYY`
#   themoviedb 详情API返回的其它一级字段
# 4. 配置多项条件时需要同时满足，一个条件需要匹配多个值是使用`,`分隔
# 5. !条件值表示排除该值

"""


class CategoryHelper(metaclass=WeakSingleton):
    """
    二级分类
    """

    def __init__(self):
        self._category_path: Path = settings.CONFIG_PATH / "category.yaml"
        self._categorys = {}
        self._movie_categorys = {}
        self._tv_categorys = {}
        self.init()

    def init(self):
        """
        初始化
        """
        try:
            if not self._category_path.exists():
                shutil.copy(settings.INNER_CONFIG_PATH / "category.yaml", self._category_path)
            with open(self._category_path, mode='r', encoding='utf-8', errors='replace') as f:
                try:
                    yaml_loader = ruamel.yaml.YAML()
                    self._categorys = yaml_loader.load(f)
                except Exception as e:
                    logger.warn(f"二级分类策略配置文件格式出现严重错误！请检查：{str(e)}")
                    self._categorys = {}
        except Exception as err:
            logger.warn(f"二级分类策略配置文件加载出错：{str(err)}")

        if self._categorys:
            self._movie_categorys = self._categorys.get('movie')
            self._tv_categorys = self._categorys.get('tv')
        logger.info(f"已加载二级分类策略 category.yaml")

    def load(self) -> CategoryConfig:
        """
        加载配置
        """
        config = CategoryConfig()
        if not self._category_path.exists():
            return config
        try:
            with open(self._category_path, 'r', encoding='utf-8', errors='replace') as f:
                yaml_loader = ruamel.yaml.YAML()
                data = yaml_loader.load(f)
                if data:
                    config = CategoryConfig(**data)
        except Exception as e:
            logger.error(f"Load category config failed: {e}")
        return config

    def save(self, config: CategoryConfig) -> bool:
        """
        保存配置
        """
        data = config.model_dump(exclude_none=True)
        try:
            with open(self._category_path, 'w', encoding='utf-8') as f:
                f.write(HEADER_COMMENTS)
                yaml_dumper = ruamel.yaml.YAML()
                yaml_dumper.dump(data, f)
            # 保存后重新加载配置
            self.init()
            return True
        except Exception as e:
            logger.error(f"Save category config failed: {e}")
            return False

    @property
    def is_movie_category(self) -> bool:
        """
        获取电影分类标志
        """
        if self._movie_categorys:
            return True
        return False

    @property
    def is_tv_category(self) -> bool:
        """
        获取电视剧分类标志
        """
        if self._tv_categorys:
            return True
        return False

    @property
    def movie_categorys(self) -> list:
        """
        获取电影分类清单
        """
        if not self._movie_categorys:
            return []
        return list(self._movie_categorys.keys())

    @property
    def tv_categorys(self) -> list:
        """
        获取电视剧分类清单
        """
        if not self._tv_categorys:
            return []
        return list(self._tv_categorys.keys())

    def get_movie_category(self, tmdb_info) -> str:
        """
        判断电影的分类
        :param tmdb_info: 识别的TMDB中的信息
        :return: 二级分类的名称
        """
        return self.get_category(self._movie_categorys, tmdb_info)

    def get_tv_category(self, tmdb_info) -> str:
        """
        判断电视剧的分类，包括动漫
        :param tmdb_info: 识别的TMDB中的信息
        :return: 二级分类的名称
        """
        return self.get_category(self._tv_categorys, tmdb_info)

    @staticmethod
    def get_category(categorys: Union[dict, CommentedMap], tmdb_info: dict) -> str:
        """
        根据 TMDB信息与分类配置文件进行比较，确定所属分类
        :param categorys: 分类配置
        :param tmdb_info: TMDB信息
        :return: 分类的名称
        """
        return CategoryHelper.evaluate_category(categorys, tmdb_info).automatic_category

    @staticmethod
    def evaluate_category(
            categorys: Union[dict, CommentedMap],
            tmdb_info: dict,
    ) -> CategoryRouteDecision:
        """
        求值全部分类规则并保留第一条命中的现有语义。

        :param categorys: 分类配置
        :param tmdb_info: 已获取的 TMDB 元数据快照
        :return: 分类规则决策与非阻断警告
        """
        if not tmdb_info or not categorys:
            return CategoryRouteDecision()

        rule_decisions = []
        fallback_indices = []
        for index, (category, raw_rule) in enumerate(categorys.items()):
            if hasattr(raw_rule, "model_dump"):
                rule = raw_rule.model_dump(exclude_none=True)
            else:
                rule = raw_rule
            rule_items = [
                (attr, value)
                for attr, value in rule.items()
                if value
            ] if rule else []
            if not rule_items:
                fallback_indices.append(index)
                rule_decisions.append(
                    CategoryRuleDecision(
                        index=index,
                        category=category,
                        matched=True,
                    )
                )
                continue

            conditions = [
                CategoryHelper._evaluate_condition(attr, value, tmdb_info)
                for attr, value in rule_items
            ]
            rule_decisions.append(
                CategoryRuleDecision(
                    index=index,
                    category=category,
                    matched=all(condition.matched for condition in conditions),
                    conditions=conditions,
                )
            )

        matched_indices = [rule.index for rule in rule_decisions if rule.matched]
        selected_index = matched_indices[0] if matched_indices else None
        if selected_index is not None:
            rule_decisions[selected_index].selected = True
            for rule in rule_decisions[selected_index + 1:]:
                rule.reachable = False

        warnings = []
        invalid_fallbacks = [
            index for index in fallback_indices if index < len(rule_decisions) - 1
        ]
        if invalid_fallbacks:
            warnings.append(
                RouteDiagnosticWarning(
                    code="unconditional_category_not_last",
                    message="无条件兜底分类不是最后一项，后续规则在实际分类中不可达",
                    related_indices=invalid_fallbacks,
                )
            )
        if len(matched_indices) > 1:
            warnings.append(
                RouteDiagnosticWarning(
                    code="multiple_category_matches",
                    message="当前媒体同时匹配多条分类规则，实际分类采用第一条",
                    related_indices=matched_indices,
                )
            )

        selected_category = (
            rule_decisions[selected_index].category
            if selected_index is not None
            else ""
        )
        return CategoryRouteDecision(
            automatic_category=selected_category,
            selected_category=selected_category,
            source="automatic" if selected_category else "none",
            rules=rule_decisions,
            warnings=warnings,
        )

    @staticmethod
    def _evaluate_condition(
            attr: str,
            value: str,
            tmdb_info: dict,
    ) -> CategoryConditionDecision:
        """求值单个分类条件并返回可展示原因。"""
        if attr == "release_year":
            info_value = tmdb_info.get("release_date") or tmdb_info.get("first_air_date")
            if info_value:
                info_value = str(info_value)[:4]
        else:
            info_value = tmdb_info.get(attr)
        if not info_value:
            return CategoryConditionDecision(
                field=attr,
                expected=value,
                actual=info_value,
                matched=False,
                message="元数据缺少该字段",
            )

        if attr == "production_countries":
            info_values = [
                str(item.get("iso_3166_1")).upper()
                for item in info_value
            ]
        elif isinstance(info_value, list):
            info_values = [str(item).upper() for item in info_value]
        else:
            info_values = [str(info_value).upper()]

        values = [item for item in str(value).split(",") if item]
        expanded_values = []
        for expected in values:
            if "-" not in expected:
                expanded_values.append(expected)
                continue
            value_begin, value_end = expected.split("-", 1)
            prefix = ""
            if value_begin.startswith("!"):
                prefix = "!"
                value_begin = value_begin[1:]
            if value_begin.isdigit() and value_end.isdigit():
                expanded_values.extend(
                    f"{prefix}{item}"
                    for item in range(int(value_begin), int(value_end) + 1)
                )
            else:
                expanded_values.extend([
                    f"{prefix}{value_begin}",
                    f"{prefix}{value_end}",
                ])

        normalized_values = [item.upper() for item in expanded_values]
        inverted_values = [item[1:] for item in normalized_values if item.startswith("!")]
        included_values = [item for item in normalized_values if not item.startswith("!")]
        included_match = (
            not included_values
            or bool(set(included_values).intersection(info_values))
        )
        excluded_match = bool(set(inverted_values).intersection(info_values))
        matched = included_match and not excluded_match
        return CategoryConditionDecision(
            field=attr,
            expected=value,
            actual=info_value,
            matched=matched,
            message="条件匹配" if matched else "元数据值不满足条件",
        )
