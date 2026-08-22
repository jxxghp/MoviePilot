from typing import Callable, Optional, List

from app.adapters.media.image import ImageHelper  # noqa: F401  推荐链路与旧插件使用的图片抓取入口
from app.adapters.network.http import RequestUtils
from app.application.configuration import get_chain_runtime_config_snapshot
from app.foundation.singleton import Singleton
from app.runtime.cache import cached


WallpaperProvider = Callable[[], Optional[str]]
WallpaperListProvider = Callable[[int], List[str]]


def _empty_wallpaper_provider() -> Optional[str]:
    """在启动组合根尚未装配壁纸来源时返回空结果。"""
    return None


def _empty_wallpaper_list_provider(_count: int) -> List[str]:
    """在启动组合根尚未装配壁纸来源时返回空列表。"""
    return []


_tmdb_wallpaper_provider: WallpaperProvider = _empty_wallpaper_provider
_tmdb_wallpaper_list_provider: WallpaperListProvider = (
    _empty_wallpaper_list_provider
)
_mediaserver_wallpaper_provider: WallpaperProvider = _empty_wallpaper_provider
_mediaserver_wallpaper_list_provider: WallpaperListProvider = (
    _empty_wallpaper_list_provider
)


def configure_wallpaper_providers(
    *,
    tmdb_wallpaper: WallpaperProvider,
    tmdb_wallpapers: WallpaperListProvider,
    mediaserver_wallpaper: WallpaperProvider,
    mediaserver_wallpapers: WallpaperListProvider,
) -> None:
    """由启动组合根注入需要业务 Chain 才能提供的壁纸来源。"""
    global _tmdb_wallpaper_provider
    global _tmdb_wallpaper_list_provider
    global _mediaserver_wallpaper_provider
    global _mediaserver_wallpaper_list_provider
    _tmdb_wallpaper_provider = tmdb_wallpaper
    _tmdb_wallpaper_list_provider = tmdb_wallpapers
    _mediaserver_wallpaper_provider = mediaserver_wallpaper
    _mediaserver_wallpaper_list_provider = mediaserver_wallpapers


class WallpaperHelper(metaclass=Singleton):
    """
    壁纸帮助类
    """

    def get_wallpaper(self) -> Optional[str]:
        """
        获取登录页面壁纸
        """
        wallpaper = get_chain_runtime_config_snapshot().wallpaper
        if wallpaper == "bing":
            return self.get_bing_wallpaper()
        elif wallpaper == "mediaserver":
            return self.get_mediaserver_wallpaper()
        elif wallpaper == "customize":
            return self.get_customize_wallpaper()
        elif wallpaper == "tmdb":
            return self.get_tmdb_wallpaper()
        return ''

    def get_wallpapers(self, num: int = 10) -> List[str]:
        """
        获取登录页面壁纸列表
        """
        wallpaper = get_chain_runtime_config_snapshot().wallpaper
        if wallpaper == "bing":
            return self.get_bing_wallpapers(num)
        elif wallpaper == "mediaserver":
            return self.get_mediaserver_wallpapers(num)
        elif wallpaper == "customize":
            return self.get_customize_wallpapers()
        elif wallpaper == "tmdb":
            return self.get_tmdb_wallpapers(num)
        return []

    @cached(maxsize=1, ttl=3600)
    def get_tmdb_wallpaper(self) -> Optional[str]:
        """
        获取TMDB每日壁纸
        """
        return _tmdb_wallpaper_provider()

    @cached(maxsize=1, ttl=3600, skip_empty=True)
    def get_tmdb_wallpapers(self, num: int = 10) -> List[str]:
        """
        获取7天的TMDB每日壁纸
        """
        return _tmdb_wallpaper_list_provider(num)

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
        return _mediaserver_wallpaper_provider()

    @cached(maxsize=1, ttl=3600, skip_empty=True)
    def get_mediaserver_wallpapers(self, num: int = 10) -> List[str]:
        """
        获取媒体服务器壁纸列表
        """
        return _mediaserver_wallpaper_list_provider(num)

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
        config = get_chain_runtime_config_snapshot()
        if config.customize_wallpaper_api_url:
            wallpaper_list = []
            resp = RequestUtils(timeout=15).get_res(config.customize_wallpaper_api_url)
            if resp and resp.status_code == 200:
                # 如果返回的是图片格式
                content_type = resp.headers.get('Content-Type')
                if content_type and content_type.lower().startswith('image/'):
                    wallpaper_list.append(config.customize_wallpaper_api_url)
                else:
                    try:
                        result = resp.json()
                        if isinstance(result, list) or isinstance(result, dict) or isinstance(result, str):
                            wallpaper_list = find_files_with_suffixes(result, config.security_image_suffixes)
                    except Exception as err:
                        print(str(err))
            return wallpaper_list
        else:
            return []
