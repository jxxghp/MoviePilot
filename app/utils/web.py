from typing import Optional, List

from app.core.cache import cached
from app.core.config import settings
from app.utils.http import RequestUtils


class WebUtils:

    @staticmethod
    def get_location(ip: str):
        """
        查询IP所属地
        """
        return WebUtils.get_location1(ip) or WebUtils.get_location2(ip)

    @staticmethod
    def get_location1(ip: str):
        """
        https://api.mir6.com/api/ip
        {
            "code": 200,
            "msg": "success",
            "data": {
                "ip": "240e:97c:2f:1::5c",
                "dec": "47925092370311863177116789888333643868",
                "country": "中国",
                "countryCode": "CN",
                "province": "广东省",
                "city": "广州市",
                "districts": "",
                "idc": "",
                "isp": "中国电信",
                "net": "数据中心",
                "zipcode": "510000",
                "areacode": "020",
                "protocol": "IPv6",
                "location": "中国[CN] 广东省 广州市",
                "myip": "125.89.7.89",
                "time": "2023-09-01 17:28:23"
            }
        }
        """
        try:
            r = RequestUtils().get_res(f"https://api.mir6.com/api/ip?ip={ip}&type=json")
            if r:
                return r.json().get("data", {}).get("location") or ''
        except Exception as err:
            print(str(err))
            return ""

    @staticmethod
    def get_location2(ip: str):
        """
        https://whois.pconline.com.cn/ipJson.jsp?json=true&ip=
        {
          "ip": "122.8.12.22",
          "pro": "上海市",
          "proCode": "310000",
          "city": "上海市",
          "cityCode": "310000",
          "region": "",
          "regionCode": "0",
          "addr": "上海市 铁通",
          "regionNames": "",
          "err": ""
        }
        """
        try:
            r = RequestUtils().get_res(f"https://whois.pconline.com.cn/ipJson.jsp?json=true&ip={ip}")
            if r:
                return r.json().get("addr") or ''
        except Exception as err:
            print(str(err))
            return ""

    @staticmethod
    @cached(maxsize=1, ttl=3600)
    def get_bing_wallpaper() -> Optional[str]:
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

    @staticmethod
    @cached(maxsize=1, ttl=3600)
    def get_bing_wallpapers(num: int = 7) -> List[str]:
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

    @staticmethod
    @cached(maxsize=1, ttl=3600)
    def get_customize_wallpapers() -> List[str]:
        """
            递归查找对象中所有包含特定后缀的文件或URL，返回匹配的字符串列表
            支持输入：字典、列表、字符串（普通文件路径或URL）
            """
        def find_files_with_suffixes(obj, suffixes: list[str]) -> list[str]:
            """
            递归查找对象中所有包含特定后缀的文件，返回匹配的字符串列表
            支持输入：字典、列表、字符串
            """
            result = []

            # 处理字符串
            if isinstance(obj, str):
                if obj.endswith(tuple(suffixes)):
                    result.append(obj)

            # 处理字典
            elif isinstance(obj, dict):
                for value in obj.values():
                    result.extend(find_files_with_suffixes(value, suffixes))

            # 处理列表
            elif isinstance(obj, list):
                for item in obj:
                    result.extend(find_files_with_suffixes(item, suffixes))

            return result
        """
        获取自定义壁纸api壁纸
        """
        # 判断是否存在自定义壁纸api
        if settings.CUSTOMIZE_WALLPAPER_API_URL is not None and len(settings.CUSTOMIZE_WALLPAPER_API_URL) > 0:
            url = settings.CUSTOMIZE_WALLPAPER_API_URL
            wallpaper_list = []
            resp = RequestUtils(timeout=5).get_res(url)
            if resp and resp.status_code == 200:
                try:
                    result = resp.json()
                    if isinstance(result, list) or isinstance(result, dict) or isinstance(result, str):
                        wallpaper_list = find_files_with_suffixes(result, settings.SECURITY_IMAGE_SUFFIXES)
                except Exception as err:
                    print(str(err))
            return wallpaper_list
        else:
            return []