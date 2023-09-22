from typing import Optional

from app.utils.http import RequestUtils


class WebUtils:
    @staticmethod
    def get_location(ip: str):
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
            return str(err)

    @staticmethod
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
