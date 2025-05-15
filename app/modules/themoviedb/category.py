import shutil
from pathlib import Path
from typing import Union

import ruamel.yaml
from ruamel.yaml import CommentedMap

from app.core.config import settings
from app.log import logger
from app.utils.singleton import Singleton


class CategoryHelper(metaclass=Singleton):
    """
    二级分类
    """
    _categorys = {}
    _movie_categorys = {}
    _tv_categorys = {}

    def __init__(self):
        self._category_path: Path = settings.CONFIG_PATH / "category.yaml"
        self.init()

    def init(self):
        """
        初始化
        """
        try:
            if not self._category_path.exists():
                shutil.copy(settings.INNER_CONFIG_PATH / "category.yaml", self._category_path)
            with open(self._category_path, mode='r', encoding='utf-8') as f:
                try:
                    yaml = ruamel.yaml.YAML()
                    self._categorys = yaml.load(f)
                except Exception as e:
                    logger.warn(f"二级分类策略配置文件格式出现严重错误！请检查：{str(e)}")
                    self._categorys = {}
        except Exception as err:
            logger.warn(f"二级分类策略配置文件加载出错：{str(err)}")

        if self._categorys:
            self._movie_categorys = self._categorys.get('movie')
            self._tv_categorys = self._categorys.get('tv')
        logger.info(f"已加载二级分类策略 category.yaml")

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
        return self._movie_categorys.keys()

    @property
    def tv_categorys(self) -> list:
        """
        获取电视剧分类清单
        """
        if not self._tv_categorys:
            return []
        return self._tv_categorys.keys()

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
                    info_values = [str(val.get("iso_3166_1")).upper() for val in info_value]
                else:
                    if isinstance(info_value, list):
                        info_values = [str(val).upper() for val in info_value]
                    else:
                        info_values = [str(info_value).upper()]

                if value.find(",") != -1:
                    # , 分隔多个值
                    values = [str(val).upper() for val in value.split(",") if val]
                elif value.find("-") != -1:
                    # - 表示范围，仅限于数字
                    value_begin = value.split("-")[0]
                    value_end = value.split("-")[1]
                    if value_begin.isdigit() and value_end.isdigit():
                        # 数字范围
                        values = [str(val) for val in range(int(value_begin), int(value_end) + 1)]
                    else:
                        # 字符串范围
                        values = [str(value_begin), str(value_end)]
                else:
                    values = [str(value).upper()]

                if not set(values).intersection(set(info_values)):
                    match_flag = False
            if match_flag:
                return key
        return ""
