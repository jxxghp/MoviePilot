from typing import Optional, List

from app.core.cache import cached
from app.core.config import settings
from app.utils.http import RequestUtils
from app.utils.singleton import Singleton


class WallpaperHelper(metaclass=Singleton):

    def __init__(self):
        self.req = RequestUtils(timeout=5)

    @cached(maxsize=1, ttl=3600)
    def get_bing_wallpaper(self) -> Optional[str]:
        """
        获取Bing每日壁纸
        """
        url = "https://cn.bing.com/HPImageArchive.aspx?format=js&idx=0&n=1"
        resp = self.req.get_res(url)
        if resp and resp.status_code == 200:
            try:
                result = resp.json()
                if isinstance(result, dict):
                    for image in result.get('images') or []:
                        return f"https://cn.bing.com{image.get('url')}" if 'url' in image else ''
            except Exception as err:
                print(str(err))
        return None

    @cached(maxsize=1, ttl=3600)
    def get_bing_wallpapers(self, num: int = 7) -> List[str]:
        """
        获取7天的Bing每日壁纸
        """
        url = f"https://cn.bing.com/HPImageArchive.aspx?format=js&idx=0&n={num}"
        resp = self.req.get_res(url)
        if resp and resp.status_code == 200:
            try:
                result = resp.json()
                if isinstance(result, dict):
                    return [f"https://cn.bing.com{image.get('url')}" for image in result.get('images') or []]
            except Exception as err:
                print(str(err))
        return []

    @cached(maxsize=1, ttl=3600)
    def get_customize_wallpaper(self) -> Optional[str]:
        """
        获取自定义壁纸api壁纸
        """
        wallpaper_list = self.get_customize_wallpapers()
        if wallpaper_list:
            return wallpaper_list[0]
        return None

    @cached(maxsize=1, ttl=3600)
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
            resp = self.req.get_res(settings.CUSTOMIZE_WALLPAPER_API_URL)
            if resp and resp.status_code == 200:
                # 如果返回的是图片格式
                content_type = resp.headers.get('Content-Type')
                if content_type and content_type.lower() == 'image/jpeg':
                    wallpaper_list.append(resp.url)
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
