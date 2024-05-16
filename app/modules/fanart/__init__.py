import re
from functools import lru_cache
from typing import Optional, Tuple, Union

from app.core.context import MediaInfo, settings
from app.log import logger
from app.modules import _ModuleBase
from app.utils.http import RequestUtils
from app.schemas.types import MediaType


class FanartModule(_ModuleBase):

    """
    {
        "name": "The Wheel of Time",
        "thetvdb_id": "355730",
        "tvposter": [
            {
                "id": "174068",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvposter/the-wheel-of-time-64b009de9548d.jpg",
                "lang": "en",
                "likes": "3"
            },
            {
                "id": "176424",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvposter/the-wheel-of-time-64de44fe42073.jpg",
                "lang": "00",
                "likes": "3"
            },
            {
                "id": "176407",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvposter/the-wheel-of-time-64dde63c7c941.jpg",
                "lang": "en",
                "likes": "0"
            },
            {
                "id": "177321",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvposter/the-wheel-of-time-64eda10599c3d.jpg",
                "lang": "cz",
                "likes": "0"
            },
            {
                "id": "155050",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvposter/the-wheel-of-time-6313adbd1fd58.jpg",
                "lang": "pl",
                "likes": "0"
            },
            {
                "id": "140198",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvposter/the-wheel-of-time-61a0d7b11952e.jpg",
                "lang": "en",
                "likes": "0"
            },
            {
                "id": "140034",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvposter/the-wheel-of-time-619e65b73871d.jpg",
                "lang": "en",
                "likes": "0"
            }
        ],
        "hdtvlogo": [
            {
                "id": "139835",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdtvlogo/the-wheel-of-time-6197d9392faba.png",
                "lang": "en",
                "likes": "3"
            },
            {
                "id": "140039",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdtvlogo/the-wheel-of-time-619e87941a128.png",
                "lang": "pt",
                "likes": "3"
            },
            {
                "id": "140092",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdtvlogo/the-wheel-of-time-619fa2347bada.png",
                "lang": "en",
                "likes": "3"
            },
            {
                "id": "164312",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdtvlogo/the-wheel-of-time-63c8185cb8824.png",
                "lang": "hu",
                "likes": "1"
            },
            {
                "id": "139827",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdtvlogo/the-wheel-of-time-6197539658a9e.png",
                "lang": "en",
                "likes": "1"
            },
            {
                "id": "177214",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdtvlogo/the-wheel-of-time-64ebae44c23a6.png",
                "lang": "cz",
                "likes": "0"
            },
            {
                "id": "177215",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdtvlogo/the-wheel-of-time-64ebae472deef.png",
                "lang": "cz",
                "likes": "0"
            },
            {
                "id": "156163",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdtvlogo/the-wheel-of-time-63316bef1ff9d.png",
                "lang": "cz",
                "likes": "0"
            },
            {
                "id": "155051",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdtvlogo/the-wheel-of-time-6313add04ca92.png",
                "lang": "pl",
                "likes": "0"
            },
            {
                "id": "152668",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdtvlogo/the-wheel-of-time-62ced3775a40a.png",
                "lang": "pl",
                "likes": "0"
            },
            {
                "id": "142266",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdtvlogo/the-wheel-of-time-61ccd93eeac2b.png",
                "lang": "de",
                "likes": "0"
            }
        ],
        "hdclearart": [
            {
                "id": "164313",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdclearart/the-wheel-of-time-63c81871c982c.png",
                "lang": "en",
                "likes": "3"
            },
            {
                "id": "140284",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdclearart/the-wheel-of-time-61a2128ed1df2.png",
                "lang": "pt",
                "likes": "3"
            },
            {
                "id": "139828",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdclearart/the-wheel-of-time-61975401e894c.png",
                "lang": "en",
                "likes": "1"
            },
            {
                "id": "164314",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdclearart/the-wheel-of-time-63c8188488a5f.png",
                "lang": "hu",
                "likes": "1"
            },
            {
                "id": "177322",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdclearart/the-wheel-of-time-64eda135933b6.png",
                "lang": "cz",
                "likes": "0"
            },
            {
                "id": "142267",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdclearart/the-wheel-of-time-61ccda9918c5c.png",
                "lang": "de",
                "likes": "0"
            }
        ],
        "seasonposter": [
            {
                "id": "140199",
                "url": "http://assets.fanart.tv/fanart/tv/355730/seasonposter/the-wheel-of-time-61a0d7c2976de.jpg",
                "lang": "en",
                "likes": "1",
                "season": "1"
            },
            {
                "id": "176395",
                "url": "http://assets.fanart.tv/fanart/tv/355730/seasonposter/the-wheel-of-time-64dd80b3d79a9.jpg",
                "lang": "en",
                "likes": "0",
                "season": "1"
            },
            {
                "id": "140035",
                "url": "http://assets.fanart.tv/fanart/tv/355730/seasonposter/the-wheel-of-time-619e65c4d5357.jpg",
                "lang": "en",
                "likes": "0",
                "season": "1"
            }
        ],
        "tvthumb": [
            {
                "id": "140242",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvthumb/the-wheel-of-time-61a1813035506.jpg",
                "lang": "en",
                "likes": "1"
            },
            {
                "id": "177323",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvthumb/the-wheel-of-time-64eda15b6dce6.jpg",
                "lang": "cz",
                "likes": "0"
            },
            {
                "id": "176399",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvthumb/the-wheel-of-time-64dd85c9b618c.jpg",
                "lang": "en",
                "likes": "0"
            },
            {
                "id": "152669",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvthumb/the-wheel-of-time-62ced53d16574.jpg",
                "lang": "pl",
                "likes": "0"
            },
            {
                "id": "141983",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvthumb/the-wheel-of-time-61c6d04a6d701.jpg",
                "lang": "en",
                "likes": "0"
            }
        ],
        "showbackground": [
            {
                "id": "177324",
                "url": "http://assets.fanart.tv/fanart/tv/355730/showbackground/the-wheel-of-time-64eda1833ccb1.jpg",
                "lang": "",
                "likes": "0",
                "season": "all"
            },
            {
                "id": "141986",
                "url": "http://assets.fanart.tv/fanart/tv/355730/showbackground/the-wheel-of-time-61c6d08f7c7e2.jpg",
                "lang": "",
                "likes": "0",
                "season": "all"
            },
            {
                "id": "139868",
                "url": "http://assets.fanart.tv/fanart/tv/355730/showbackground/the-wheel-of-time-6198ce358b98a.jpg",
                "lang": "",
                "likes": "0",
                "season": "all"
            }
        ],
        "seasonthumb": [
            {
                "id": "176396",
                "url": "http://assets.fanart.tv/fanart/tv/355730/seasonthumb/the-wheel-of-time-64dd80c8593f9.jpg",
                "lang": "en",
                "likes": "0",
                "season": "1"
            },
            {
                "id": "176400",
                "url": "http://assets.fanart.tv/fanart/tv/355730/seasonthumb/the-wheel-of-time-64dd85da7c5e9.jpg",
                "lang": "en",
                "likes": "0",
                "season": "0"
            }
        ],
        "tvbanner": [
            {
                "id": "176397",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvbanner/the-wheel-of-time-64dd80da9a255.jpg",
                "lang": "en",
                "likes": "0"
            },
            {
                "id": "176401",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvbanner/the-wheel-of-time-64dd85e8904ea.jpg",
                "lang": "en",
                "likes": "0"
            },
            {
                "id": "141988",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvbanner/the-wheel-of-time-61c6d34bceb5f.jpg",
                "lang": "en",
                "likes": "0"
            },
            {
                "id": "141984",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvbanner/the-wheel-of-time-61c6d06c1c21c.jpg",
                "lang": "en",
                "likes": "0"
            }
        ],
        "seasonbanner": [
            {
                "id": "176398",
                "url": "http://assets.fanart.tv/fanart/tv/355730/seasonbanner/the-wheel-of-time-64dd80e7dbd9f.jpg",
                "lang": "en",
                "likes": "0",
                "season": "1"
            },
            {
                "id": "176402",
                "url": "http://assets.fanart.tv/fanart/tv/355730/seasonbanner/the-wheel-of-time-64dd85fb4f1b1.jpg",
                "lang": "en",
                "likes": "0",
                "season": "0"
            }
        ]
    }
    """

    # 代理
    _proxies: dict = settings.PROXY

    # Fanart Api
    _movie_url: str = f'https://webservice.fanart.tv/v3/movies/%s?api_key={settings.FANART_API_KEY}'
    _tv_url: str = f'https://webservice.fanart.tv/v3/tv/%s?api_key={settings.FANART_API_KEY}'

    def init_module(self) -> None:
        pass

    def stop(self):
        pass

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        ret = RequestUtils().get_res("https://webservice.fanart.tv")
        if ret and ret.status_code == 200:
            return True, ""
        elif ret:
            return False, f"无法连接fanart，错误码：{ret.status_code}"
        return False, "fanart网络连接失败"

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        return "FANART_API_KEY", True

    @staticmethod
    def get_name() -> str:
        return "Fanart"

    def obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        获取图片
        :param mediainfo:  识别的媒体信息
        :return: 更新后的媒体信息
        """
        if not settings.FANART_ENABLE:
            return None
        if not mediainfo.tmdb_id and not mediainfo.tvdb_id:
            return None
        if mediainfo.type == MediaType.MOVIE:
            result = self.__request_fanart(mediainfo.type, mediainfo.tmdb_id)
        else:
            if mediainfo.tvdb_id:
                result = self.__request_fanart(mediainfo.type, mediainfo.tvdb_id)
            else:
                logger.info(f"{mediainfo.title_year} 没有tvdbid，无法获取fanart图片")
                return None
        if not result or result.get('status') == 'error':
            logger.warn(f"没有获取到 {mediainfo.title_year} 的fanart图片数据")
            return None
        # 获取所有图片
        for name, images in result.items():
            if not images:
                continue
            if not isinstance(images, list):
                continue
            # 按欢迎程度倒排
            images.sort(key=lambda x: int(x.get('likes', 0)), reverse=True)
            # 取第一张图片
            image_obj = images[0]
            # 图片属性xx_path
            image_name = self.__name(name)
            image_season = image_obj.get('season')
            # 设置图片
            if image_name.startswith("season") and image_season:
                # 季图片格式 seasonxx-poster
                image_name = f"season{str(image_season).rjust(2, '0')}-{image_name[6:]}"
            if not mediainfo.get_image(image_name):
                # 没有图片才设置
                mediainfo.set_image(image_name, image_obj.get('url'))

        return mediainfo

    @staticmethod
    def __name(fanart_name: str) -> str:
        """
        转换Fanart图片的名字
        """
        words_to_remove = r'tv|movie|hdmovie|hdtv|show|hd'
        pattern = re.compile(words_to_remove, re.IGNORECASE)
        result = re.sub(pattern, '', fanart_name)
        return result

    @classmethod
    @lru_cache(maxsize=settings.CACHE_CONF.get('fanart'))
    def __request_fanart(cls, media_type: MediaType, queryid: Union[str, int]) -> Optional[dict]:
        if media_type == MediaType.MOVIE:
            image_url = cls._movie_url % queryid
        else:
            image_url = cls._tv_url % queryid
        try:
            ret = RequestUtils(proxies=cls._proxies, timeout=10).get_res(image_url)
            if ret:
                return ret.json()
        except Exception as err:
            logger.error(f"获取{queryid}的Fanart图片失败：{str(err)}")
        return None
