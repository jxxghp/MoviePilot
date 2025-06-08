from typing import Optional, List

from app.chain.mediaserver import MediaServerChain
from app.chain.tmdb import TmdbChain
from app.core.cache import cached
from app.core.config import settings
from app.utils.http import RequestUtils
from app.utils.singleton import Singleton


class WallpaperHelper(metaclass=Singleton):
    """
    壁纸帮助类
    """

    def get_wallpaper(self) -> Optional[str]:
        """
        获取登录页面壁纸
        """
        if settings.WALLPAPER == "bing":
            url = self.get_bing_wallpaper()
        elif settings.WALLPAPER == "mediaserver":
            url = self.get_mediaserver_wallpaper()
        elif settings.WALLPAPER == "customize":
            url = self.get_customize_wallpaper()
        else:
            url = self.get_tmdb_wallpaper()
        return url

    def get_wallpapers(self, num: int = 10) -> List[str]:
        """
        获取登录页面壁纸列表
        """
        if settings.WALLPAPER == "bing":
            return self.get_bing_wallpapers(num)
        elif settings.WALLPAPER == "mediaserver":
            return self.get_mediaserver_wallpapers(num)
        elif settings.WALLPAPER == "customize":
            return self.get_customize_wallpapers()
        else:
            return self.get_tmdb_wallpapers(num)

    @cached(maxsize=1, ttl=3600)
    def get_tmdb_wallpaper(self) -> Optional[str]:
        """
        获取TMDB每日壁纸
        """
        return TmdbChain().get_random_wallpager()

    @cached(maxsize=1, ttl=3600, skip_empty=True)
    def get_tmdb_wallpapers(self, num: int = 10) -> List[str]:
        """
        获取7天的TMDB每日壁纸
        """
        return TmdbChain().get_trending_wallpapers(num)

    @cached(maxsize=1, ttl=3600)
    def get_bing_wallpaper(self) -> Optional[str]:
        """
        获取Bing每日壁纸
        """
        url = "https://cn.bing.com/HPImageArchive.aspx?format=js&idx=0&n=1"
        resp = RequestUtils(timeout=5).get_res(url)
        if resp and resp.status_code == 200:
            try:
                result = resp.json()
                if isinstance(result, dict):
                    for image in result.get('images') or []:
                        return f"https://cn.bing.com{image.get('url')}" if 'url' in image else ''
            except Exception as err:
                print(str(err))
        return None

    @cached(maxsize=1, ttl=3600, skip_empty=True)
    def get_bing_wallpapers(self, num: int = 7) -> List[str]:
        """
        获取7天的Bing每日壁纸
        """
        url = f"https://cn.bing.com/HPImageArchive.aspx?format=js&idx=0&n={num}"
        resp = RequestUtils(timeout=5).get_res(url)
        if resp and resp.status_code == 200:
            try:
                result = resp.json()
                if isinstance(result, dict):
                    return [f"https://cn.bing.com{image.get('url')}" for image in result.get('images') or []]
            except Exception as err:
                print(str(err))
        return []

    @cached(maxsize=1, ttl=3600)
    def get_mediaserver_wallpaper(self) -> Optional[str]:
        """
        获取媒体服务器壁纸
        """
        return MediaServerChain().get_latest_wallpaper()

    @cached(maxsize=1, ttl=3600, skip_empty=True)
    def get_mediaserver_wallpapers(self, num: int = 10) -> List[str]:
        """
        获取媒体服务器壁纸列表
        """
        return MediaServerChain().get_latest_wallpapers(count=num)

    @cached(maxsize=1, ttl=3600)
    def get_customize_wallpaper(self) -> Optional[str]:
        """
        获取自定义壁纸api壁纸
        """
        wallpaper_list = self.get_customize_wallpapers()
        if wallpaper_list:
            return wallpaper_list[0]
        return None

    @cached(maxsize=1, ttl=3600, skip_empty=True)
    def get_customize_wallpapers(self) -> List[str]:
        """
        获取自定义壁纸api壁纸
        """

        def find_files_with_suffixes(obj, suffixes: List[str]) -> List[str]:
            """
            递归查找对象中所有包含特定后缀的文件，返回匹配的字符串列表
            支持输入：字典、列表、字符串
            """
            _result = []

            # 处理字符串
            if isinstance(obj, str):
                if obj.endswith(tuple(suffixes)):
                    _result.append(obj)

            # 处理字典
            elif isinstance(obj, dict):
                for value in obj.values():
                    _result.extend(find_files_with_suffixes(value, suffixes))

            # 处理列表
            elif isinstance(obj, list):
                for item in obj:
                    _result.extend(find_files_with_suffixes(item, suffixes))

            return _result

        # 判断是否存在自定义壁纸api
        if settings.CUSTOMIZE_WALLPAPER_API_URL:
            wallpaper_list = []
            resp = RequestUtils(timeout=15).get_res(settings.CUSTOMIZE_WALLPAPER_API_URL)
            if resp and resp.status_code == 200:
                # 如果返回的是图片格式
                content_type = resp.headers.get('Content-Type')
                if content_type and content_type.lower().startswith('image/'):
                    wallpaper_list.append(settings.CUSTOMIZE_WALLPAPER_API_URL)
                else:
                    try:
                        result = resp.json()
                        if isinstance(result, list) or isinstance(result, dict) or isinstance(result, str):
                            wallpaper_list = find_files_with_suffixes(result, settings.SECURITY_IMAGE_SUFFIXES)
                    except Exception as err:
                        print(str(err))
            return wallpaper_list
        else:
            return []
