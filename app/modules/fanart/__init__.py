import asyncio
import re
from pathlib import Path
from typing import Optional, Tuple, Union

from app.core.cache import cached
from app.core.context import MediaInfo, settings
from app.log import logger
from app.modules import _ModuleBase
from app.schemas.types import MediaType, ModuleType, OtherModulesType
from app.utils.http import RequestUtils, AsyncRequestUtils


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
    _movie_url: str = (
        f"https://webservice.fanart.tv/v3/movies/%s?api_key={settings.FANART_API_KEY}"
    )
    _tv_url: str = (
        f"https://webservice.fanart.tv/v3/tv/%s?api_key={settings.FANART_API_KEY}"
    )

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

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型
        """
        return ModuleType.Other

    @staticmethod
    def get_subtype() -> OtherModulesType:
        """
        获取模块子类型
        """
        return OtherModulesType.Fanart

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 0

    def obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        获取图片
        :param mediainfo:  识别的媒体信息
        :return: 更新后的媒体信息
        """
        images = self.__obtain_fanart_images(mediainfo=mediainfo)
        if not images:
            return None

        self.__set_mediainfo_images(mediainfo=mediainfo, images=images)
        return mediainfo

    async def async_obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        获取图片（异步版本）
        :param mediainfo:  识别的媒体信息
        :return: 更新后的媒体信息
        """
        images = await self.__async_obtain_fanart_images(mediainfo=mediainfo)
        if not images:
            return None

        self.__set_mediainfo_images(mediainfo=mediainfo, images=images)
        return mediainfo

    @classmethod
    def __set_mediainfo_images(cls, mediainfo: MediaInfo, images: dict) -> None:
        """
        显式回填 MediaInfo 支持的展示图片字段
        """
        for image_name, image_url in images.items():
            image_attr = cls.__mediainfo_image_attr(image_name)
            if image_attr and not getattr(mediainfo, image_attr, None):
                setattr(mediainfo, image_attr, image_url)
                logger.debug(f"{mediainfo.title_year} 使用 Fanart 图片回填 {image_attr}：{image_name}")

    def metadata_img(
        self,
        mediainfo: MediaInfo,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> Optional[dict]:
        """
        获取图片名称和url
        :param mediainfo: 媒体信息
        :param season: 季号
        :param episode: 集号
        """
        if episode is not None:
            # Fanart 没有集图片
            return None
        return self.__obtain_fanart_images(mediainfo=mediainfo, season=season)

    def __obtain_fanart_images(
        self, mediainfo: MediaInfo, season: Optional[int] = None
    ) -> Optional[dict]:
        """
        获取 Fanart 图片并转换为刮削图片名称
        """
        query = self.__fanart_query(mediainfo=mediainfo)
        if not query:
            return None
        result = self.__request_fanart(*query)
        return self.__extract_images(mediainfo=mediainfo, result=result, season=season)

    async def __async_obtain_fanart_images(
        self,
        mediainfo: MediaInfo,
        season: Optional[int] = None,
    ) -> Optional[dict]:
        """
        异步获取 Fanart 图片并转换为刮削图片名称
        """
        query = self.__fanart_query(mediainfo=mediainfo)
        if not query:
            return None
        result = await self.__async_request_fanart(*query)
        return self.__extract_images(mediainfo=mediainfo, result=result, season=season)

    @staticmethod
    def __fanart_query(
        mediainfo: MediaInfo,
    ) -> Optional[Tuple[MediaType, Union[str, int]]]:
        """
        获取 Fanart 查询参数
        """
        if not settings.FANART_ENABLE:
            return None
        if not mediainfo.tmdb_id and not mediainfo.tvdb_id:
            return None
        if mediainfo.type == MediaType.MOVIE:
            return mediainfo.type, mediainfo.tmdb_id
        if mediainfo.tvdb_id:
            return mediainfo.type, mediainfo.tvdb_id
        logger.info(f"{mediainfo.title_year} 没有tvdbid，无法获取fanart图片")
        return None

    def __extract_images(
        self,
        mediainfo: MediaInfo,
        result: Optional[dict],
        season: Optional[int] = None,
    ) -> Optional[dict]:
        """
        从 Fanart 响应中提取图片名称和地址
        """
        if not result or result.get("status") == "error":
            logger.warn(f"没有获取到 {mediainfo.title_year} 的fanart图片数据")
            return None

        ret = {}
        # 获取所有图片
        for name, images in result.items():
            if not images:
                continue
            if not isinstance(images, list):
                continue

            # 图片属性xx_path
            image_name = self.__name(name)
            if image_name.startswith("season"):
                image_type = image_name[6:]
                # 季图片，图片格式seasonxx-xxxx/season-specials-xxxx
                for image_obj in images:
                    image_season = image_obj.get("season")
                    if image_season is not None:
                        if season is not None and str(image_season) != str(season):
                            continue
                        # 包括poster,thumb,banner
                        if image_season == "0":
                            season_image = f"season-specials-{image_type}"
                        else:
                            season_image = (
                                f"season{str(image_season).rjust(2, '0')}-{image_type}"
                            )
                        if image_url := image_obj.get("url"):
                            ret.setdefault(
                                f"{season_image}{Path(image_url).suffix}", image_url
                            )
            else:
                if season is not None:
                    continue
                image_obj = self.__pick_best_image(images)
                if image_url := image_obj.get("url"):
                    ret[f"{image_name}{Path(image_url).suffix}"] = image_url

        return ret or None

    @staticmethod
    def __mediainfo_image_attr(image_name: str) -> Optional[str]:
        """
        将 Fanart 刮削图片名映射为 MediaInfo 的显式图片字段
        """
        image_key = Path(image_name).stem
        if image_key == "poster":
            return "poster_path"
        if image_key in ("background", "fanart", "backdrop"):
            return "backdrop_path"
        if image_key == "logo":
            return "logo_path"
        return None

    @staticmethod
    def __pick_best_image(_images: list):
        """
        其他图片，优先环境变量指定语言，再like最多
        """
        lang_env = settings.FANART_LANG
        if lang_env:
            langs = [lang.strip() for lang in lang_env.split(",") if lang.strip()]
            for lang in langs:
                lang_images = [img for img in _images if img.get("lang") == lang]
                if lang_images:
                    lang_images.sort(key=lambda x: int(x.get("likes", 0)), reverse=True)
                    return lang_images[0]
        # 没设置或没找到，按原逻辑 zh、en、like最多
        zh_images = [img for img in _images if img.get("lang") == "zh"]
        if zh_images:
            zh_images.sort(key=lambda x: int(x.get("likes", 0)), reverse=True)
            return zh_images[0]
        en_images = [img for img in _images if img.get("lang") == "en"]
        if en_images:
            en_images.sort(key=lambda x: int(x.get("likes", 0)), reverse=True)
            return en_images[0]
        _images.sort(key=lambda x: int(x.get("likes", 0)), reverse=True)
        return _images[0]

    @staticmethod
    def __name(fanart_name: str) -> str:
        """
        转换Fanart图片的名字
        """
        words_to_remove = r"tv|movie|hdmovie|hdtv|show|hd"
        pattern = re.compile(words_to_remove, re.IGNORECASE)
        result = re.sub(pattern, "", fanart_name)
        return result

    @classmethod
    @cached(maxsize=settings.CONF.fanart, ttl=settings.CONF.meta, shared_key="get")
    def __request_fanart(
        cls, media_type: MediaType, queryid: Union[str, int]
    ) -> Optional[dict]:
        image_url = cls.__fanart_url(media_type=media_type, queryid=queryid)
        try:
            ret = RequestUtils(proxies=cls._proxies, timeout=10).get_res(
                image_url, raise_exception=True
            )
            if ret:
                return ret.json()
            else:
                logger.debug(f"未能获取到 {queryid} 的Fanart图片")
                return {}
        except Exception as err:
            logger.error(f"获取{queryid}的Fanart图片失败：{str(err)}")
            return None

    @classmethod
    @cached(maxsize=settings.CONF.fanart, ttl=settings.CONF.meta, shared_key="get")
    async def __async_request_fanart(
        cls, media_type: MediaType, queryid: Union[str, int]
    ) -> Optional[dict]:
        image_url = cls.__fanart_url(media_type=media_type, queryid=queryid)
        try:
            ret = await AsyncRequestUtils(proxies=cls._proxies, timeout=10).get_json(
                image_url
            )
            if ret:
                return ret
            logger.debug(f"未能获取到 {queryid} 的Fanart图片")
            return {}
        except Exception as err:
            logger.error(f"获取{queryid}的Fanart图片失败：{str(err)}")
            return None

    @classmethod
    def __fanart_url(cls, media_type: MediaType, queryid: Union[str, int]) -> str:
        """
        生成 Fanart 请求地址
        """
        if media_type == MediaType.MOVIE:
            return cls._movie_url % queryid
        return cls._tv_url % queryid

    def clear_cache(self):
        """
        清除缓存
        """
        logger.info(f"开始清除{self.get_name()}缓存 ...")
        self.__request_fanart.cache_clear()
        async_cache_clear = self.__async_request_fanart.cache_clear()
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(async_cache_clear)
        except RuntimeError:
            asyncio.run(async_cache_clear)
        logger.info(f"{self.get_name()}缓存清除完成")
