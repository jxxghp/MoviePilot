import shutil
from pathlib import Path

import ruamel.yaml

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
    _anime_categorys = {}

    def __init__(self):
        self._category_path: Path = settings.CONFIG_PATH / "category.yaml"
        self.init()

    def init(self):
        """
        初始化
        """
        # 二级分类策略关闭
        if not settings.LIBRARY_CATEGORY and not settings.DOWNLOAD_CATEGORY:
            return
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
            self._anime_categorys = self._categorys.get('anime')
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

    @property
    def anime_categorys(self) -> list:
        """
        获取动漫分类清单
        """
        if not self._anime_categorys:
            return []
        return self._anime_categorys.keys()

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
        genre_ids = tmdb_info.get("genre_ids") or []
        if self._anime_categorys and genre_ids \
                and set(genre_ids).intersection(set(settings.ANIME_GENREIDS)):
            return self.get_category(self._anime_categorys, tmdb_info)
        return self.get_category(self._tv_categorys, tmdb_info)

    @staticmethod
    def get_category(categorys: dict, tmdb_info: dict) -> str:
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
                info_value = tmdb_info.get(attr)
                if not info_value:
                    match_flag = False
                    continue
                elif attr == "production_countries":
                    info_values = [str(val.get("iso_3166_1")).upper() for val in info_value]
                else:
                    if isinstance(info_value, list):
                        info_values = [str(val).upper() for val in info_value]
                    else:
                        info_values = [str(info_value).upper()]

                if value.find(",") != -1:
                    values = [str(val).upper() for val in value.split(",")]
                else:
                    values = [str(value).upper()]

                if not set(values).intersection(set(info_values)):
                    match_flag = False
            if match_flag:
                return key
        return ""
