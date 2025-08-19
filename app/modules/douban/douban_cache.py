import time
from typing import Optional

from app.core.meta import MetaBase
from app.core.metainfo import MetaInfo
from app.log import logger
from app.utils.singleton import WeakSingleton
from app.schemas.types import MediaType
from app.helper.cache_manager import douban_cache_manager

CACHE_EXPIRE_TIMESTAMP_STR = "cache_expire_timestamp"


class DoubanCache(metaclass=WeakSingleton):
    """
    豆瓣缓存数据
    {
        "id": '',
        "title": '',
        "year": '',
        "type": MediaType
    }
    """
    # 豆瓣缓存过期
    _douban_cache_expire: bool = True

    def __init__(self):
        pass

    def clear(self):
        """
        清空所有豆瓣缓存
        """
        douban_cache_manager.clear()

    def get(self, meta: MetaBase):
        """
        根据KEY值获取缓存值
        """
        info = douban_cache_manager.get_by_meta(meta)
        if info:
            expire = info.get(CACHE_EXPIRE_TIMESTAMP_STR)
            if not expire or int(time.time()) < expire:
                # 更新过期时间
                info[CACHE_EXPIRE_TIMESTAMP_STR] = int(time.time()) + (24 * 3600)  # 24小时
                douban_cache_manager.set_by_meta(meta, info)
            elif expire and self._douban_cache_expire:
                douban_cache_manager.delete_by_meta(meta)
        return info or {}

    def delete(self, key: str) -> dict:
        """
        删除缓存信息
        @param key: 缓存key
        @return: 被删除的缓存内容
        """
        # 这里需要根据key找到对应的meta对象，暂时返回空字典
        # 实际使用中应该通过meta对象来删除缓存
        return {}

    def delete_by_doubanid(self, doubanid: str) -> None:
        """
        清空对应豆瓣ID的所有缓存记录，以强制更新豆瓣中最新的数据
        """
        douban_cache_manager.delete_by_doubanid(doubanid)

    def delete_unknown(self) -> None:
        """
        清除未识别的缓存记录，以便重新搜索豆瓣
        """
        douban_cache_manager.delete_unknown()

    def modify(self, key: str, title: str) -> dict:
        """
        修改缓存信息
        @param key: 缓存key
        @param title: 标题
        @return: 被修改后缓存内容
        """
        # 这里需要根据key找到对应的meta对象，暂时返回空字典
        # 实际使用中应该通过meta对象来修改缓存
        return {}

    def update(self, meta: MetaBase, info: dict) -> None:
        """
        新增或更新缓存条目
        """
        if info:
            # 缓存标题
            cache_title = info.get("title")
            # 缓存年份
            cache_year = info.get('year')
            # 类型
            if isinstance(info.get('media_type'), MediaType):
                mtype = info.get('media_type')
            elif info.get("type"):
                mtype = MediaType.MOVIE if info.get("type") == "movie" else MediaType.TV
            else:
                meta_info = MetaInfo(cache_title)
                if meta_info.begin_season:
                    mtype = MediaType.TV
                else:
                    mtype = MediaType.MOVIE
            # 海报
            poster_path = info.get("pic", {}).get("large")
            if not poster_path and info.get("cover_url"):
                poster_path = info.get("cover_url")
            if not poster_path and info.get("cover"):
                poster_path = info.get("cover").get("url")

            cache_data = {
                "id": info.get("id"),
                "type": mtype,
                "year": cache_year,
                "title": cache_title,
                "poster_path": poster_path,
                CACHE_EXPIRE_TIMESTAMP_STR: int(time.time()) + (24 * 3600)  # 24小时
            }
            douban_cache_manager.set_by_meta(meta, cache_data)
        elif info is not None:
            # None时不缓存，此时代表网络错误，允许重复请求
            douban_cache_manager.set_by_meta(meta, {'id': "0"})

    def save(self, force: Optional[bool] = False) -> None:
        """
        保存缓存数据到文件（已由CacheManager自动处理）
        """
        pass

    def get_title(self, key: str) -> Optional[str]:
        """
        获取缓存的标题
        """
        # 这里需要根据key找到对应的meta对象，暂时返回None
        # 实际使用中应该通过meta对象来获取缓存
        return None

    def set_title(self, key: str, cn_title: str) -> None:
        """
        重新设置缓存标题
        """
        # 这里需要根据key找到对应的meta对象
        # 实际使用中应该通过meta对象来设置缓存
        pass
