import shutil
from pathlib import Path
from typing import Union

import ruamel.yaml
from ruamel.yaml import CommentedMap

from app.runtime.config import settings
from app.runtime.log import logger
from app.schemas.category import CategoryConfig
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
        if not tmdb_info:
            return ""
        if not categorys:
            return ""

        for key, item in categorys.items():
            if not item:
                return key
            match_flag = True
            for attr, value in item.items():
                if not value:
                    continue
                if attr == "release_year":
                    # 发行年份
                    info_value = tmdb_info.get("release_date") or tmdb_info.get("first_air_date")
                    if info_value:
                        info_value = str(info_value)[:4]
                else:
                    info_value = tmdb_info.get(attr)
                if not info_value:
                    match_flag = False
                    continue
                elif attr == "production_countries":
                    # 制片国家
                    info_values = [str(val.get("iso_3166_1")).upper() for val in info_value]  # type: ignore
                else:
                    if isinstance(info_value, list):
                        info_values = [str(val).upper() for val in info_value]
                    else:
                        info_values = [str(info_value).upper()]

                values = []
                invert_values = []

                # 如果有 "," 进行分割
                values = [str(val) for val in value.split(",") if val]

                expanded_values = []
                for v in values:
                    if "-" not in v:
                        expanded_values.append(v)
                        continue

                    # - 表示范围
                    value_begin, value_end = v.split("-", 1)

                    prefix = ""
                    if value_begin.startswith('!'):
                        prefix = '!'
                        value_begin = value_begin[1:]

                    if value_begin.isdigit() and value_end.isdigit():
                        # 数字范围
                        expanded_values.extend(f"{prefix}{val}" for val in range(int(value_begin), int(value_end) + 1))
                    else:
                        # 字符串范围
                        expanded_values.extend([f"{prefix}{value_begin}", f"{prefix}{value_end}"])

                values = list(map(str.upper, expanded_values))

                invert_values = [val[1:] for val in values if val.startswith('!')]
                values = [val for val in values if not val.startswith('!')]

                if values and not set(values).intersection(set(info_values)):
                    match_flag = False
                if invert_values and set(invert_values).intersection(set(info_values)):
                    match_flag = False
            if match_flag:
                return key
        return ""
