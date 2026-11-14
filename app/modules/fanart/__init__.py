"""
Fanart 图片查询、语言选择和媒体刮削清单生成。
"""

import asyncio
import hashlib
from pathlib import Path
from typing import Any, Optional, Tuple, Union, cast
from urllib.parse import urlsplit

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.domain.context import MediaInfo
from app.modules import _ModuleBase
from app.runtime.cache import AsyncCacheControls, CacheControls, cached
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.runtime.tasks import get_task_registry
from app.schemas.types import MediaType, ModuleType, OtherModulesType


class FanartModule(_ModuleBase):
    """
    从 Fanart 补充媒体展示图片，并生成媒体服务器兼容的刮削图片清单。

    电影使用 TMDB 标识，电视剧使用 TVDB 标识。普通图片按语言偏好和点赞数选择，
    季图片按季号取服务端首次提供的同名资源，不支持分集图片。
    """

    def init_module(self) -> None:
        """
        初始化 Fanart 模块。

        请求缓存键包含 API Key 指纹，更换 Key 后自然使用新键，无需在每次启动或重载时清空缓存。
        """

    def stop(self) -> None:
        """
        停止 Fanart 模块；请求资源由每次调用按当前配置创建。
        """

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性。
        """
        ret = RequestUtils().get_res("https://webservice.fanart.tv")
        if ret and ret.status_code == 200:
            return True, ""
        elif ret:
            return False, f"无法连接fanart，错误码：{ret.status_code}"
        return False, "fanart网络连接失败"

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        """
        由 Fanart 总开关控制模块是否参与宿主运行时。
        """
        return "FANART_ENABLE", True

    @staticmethod
    def get_name() -> str:
        """
        返回 Fanart 模块的稳定名称。
        """
        return "Fanart"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型。
        """
        return ModuleType.Other

    @staticmethod
    def get_subtype() -> OtherModulesType:
        """
        获取模块子类型。
        """
        return OtherModulesType.Fanart

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。
        """
        return 0

    def obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        获取 Fanart 图片并填充媒体当前缺失的展示图片字段。

        已有图片不覆盖，其他 Fanart 图片仅用于刮削清单，不写入未声明字段。

        :param mediainfo: 待补充的媒体对象，将原地修改海报、背景图或标志字段
        :return: 原媒体对象；模块关闭、缺少身份或没有图片时为 None
        """
        images = self.__obtain_fanart_images(mediainfo=mediainfo)
        if not images:
            return None

        self.__set_mediainfo_images(mediainfo=mediainfo, images=images)
        return mediainfo

    async def async_obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        异步获取 Fanart 图片并填充媒体当前缺失的展示图片字段。

        已有图片不覆盖，其他 Fanart 图片仅用于刮削清单，不写入未声明字段。

        :param mediainfo: 待补充的媒体对象，将原地修改海报、背景图或标志字段
        :return: 原媒体对象；模块关闭、缺少身份或没有图片时为 None
        """
        images = await self.__async_obtain_fanart_images(mediainfo=mediainfo)
        if not images:
            return None

        self.__set_mediainfo_images(mediainfo=mediainfo, images=images)
        return mediainfo

    @classmethod
    def __set_mediainfo_images(
        cls, mediainfo: MediaInfo, images: dict[str, Any]
    ) -> None:
        """
        显式回填 MediaInfo 支持的展示图片字段。
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
    ) -> Optional[dict[str, Any]]:
        """
        查询媒体级或指定季的图片清单。

        :param mediainfo: 含来源标识的统一媒体信息
        :param season: 指定季号；None 包含媒体级及所有季图片
        :param episode: 分集图片请求，非 None 时直接返回 None
        :return: 图片文件名与下载 URL 映射；模块关闭、缺少身份或没有图片时为 None
        """
        if episode is not None:
            # Fanart 没有集图片
            return None
        return self.__obtain_fanart_images(mediainfo=mediainfo, season=season)

    def __obtain_fanart_images(
        self, mediainfo: MediaInfo, season: Optional[int] = None
    ) -> Optional[dict[str, Any]]:
        """
        获取 Fanart 图片并转换为刮削图片名称。
        """
        query = self.__fanart_query(mediainfo=mediainfo)
        if not query:
            return None
        result = self.__request_fanart(*query, _key_token=self.__key_token())
        return self.__extract_images(mediainfo=mediainfo, result=result, season=season)

    async def __async_obtain_fanart_images(
        self,
        mediainfo: MediaInfo,
        season: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        """
        异步获取 Fanart 图片并转换为刮削图片名称。
        """
        query = self.__fanart_query(mediainfo=mediainfo)
        if not query:
            return None
        result = await self.__async_request_fanart(*query, _key_token=self.__key_token())
        return self.__extract_images(mediainfo=mediainfo, result=result, season=season)

    @staticmethod
    def __fanart_query(
        mediainfo: MediaInfo,
    ) -> Optional[Tuple[MediaType, Union[str, int]]]:
        """
        获取 Fanart 查询参数。
        """
        if not get_runtime_setting('FANART_ENABLE'):
            return None
        if not mediainfo.tmdb_id and not mediainfo.tvdb_id:
            return None
        if mediainfo.type == MediaType.MOVIE and mediainfo.tmdb_id is not None:
            return MediaType.MOVIE, mediainfo.tmdb_id
        if mediainfo.tvdb_id:
            return MediaType.TV, mediainfo.tvdb_id
        logger.info(f"{mediainfo.title_year} 没有tvdbid，无法获取fanart图片")
        return None

    def __extract_images(
        self,
        mediainfo: MediaInfo,
        result: Optional[dict[str, Any]],
        season: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        """
        把服务端图片类型转换为刮削文件名，并筛选所需季号。

        季图片中同名文件只保留首次出现的地址，特别篇使用 season-specials 前缀。

        :param mediainfo: 用于失败日志的媒体信息
        :param result: Fanart 原始响应
        :param season: 指定季号；None 同时保留媒体级与所有季图片
        :return: 文件名到 URL 的映射；响应失败或没有可用图片时为 None
        """
        if not result or result.get("status") == "error":
            logger.warn(f"没有获取到 {mediainfo.title_year} 的fanart图片数据")
            return None

        ret: dict[str, Any] = {}
        for name, images in result.items():
            if not images:
                continue
            if not isinstance(images, list):
                continue

            if (
                name.lower() in {"show4kbackground", "movie4kbackground"}
                and not get_runtime_setting("SCRAP_4K_BACKGROUND", True)
            ):
                continue

            image_name = self.__name(name)
            if image_name.startswith("season"):
                image_type = image_name[6:]
                # 季图片，图片格式seasonxx-xxxx/season-specials-xxxx
                for image_obj in images:
                    image_season = image_obj.get("season")
                    if image_season is not None:
                        if season is not None and str(image_season) != str(season):
                            continue
                        if image_season == "0":
                            season_image = f"season-specials-{image_type}"
                        else:
                            season_image = (
                                f"season{str(image_season).rjust(2, '0')}-{image_type}"
                            )
                        if image_url := image_obj.get("url"):
                            ret.setdefault(
                                f"{season_image}{self.__image_suffix(image_url)}", image_url
                            )
            else:
                if season is not None:
                    continue
                image_obj = self.__pick_best_image(images)
                if image_url := image_obj.get("url"):
                    ret[f"{image_name}{self.__image_suffix(image_url)}"] = image_url

        return ret or None

    @staticmethod
    def __mediainfo_image_attr(image_name: str) -> Optional[str]:
        """
        将 Fanart 刮削图片名映射为 MediaInfo 的显式图片字段。
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
    def __pick_best_image(_images: list[dict[str, Any]]) -> dict[str, Any]:
        """
        按配置语言顺序选择图片，再按点赞数从高到低选择。

        配置语言无结果时依次回退中文、英文和全部图片。
        回退到全部图片时会原地排序输入列表。

        :param _images: 非空图片列表，候选需提供可转为整数的 likes 值
        :return: 选中的原始图片记录
        """
        lang_env = get_runtime_setting('FANART_LANG')
        if lang_env:
            langs = [lang.strip() for lang in lang_env.split(",") if lang.strip()]
            for lang in langs:
                lang_images = [img for img in _images if img.get("lang") == lang]
                if lang_images:
                    lang_images.sort(key=lambda x: int(x.get("likes", 0)), reverse=True)
                    return lang_images[0]
        # 自定义语言没有命中时，依次尝试中文、英文和点赞数最高的图片。
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

    _FANART_NAME_MAP = {
        "showbackground": "fanart",
        "moviebackground": "fanart",
        "show4kbackground": "background",
        "movie4kbackground": "background",
        "hdtvlogo": "logo",
        "hdmovielogo": "logo",
        "movielogo": "logo",
        "clearlogo": "logo",
        "tvposter": "poster",
        "movieposter": "poster",
        "tvthumb": "thumb",
        "moviethumb": "thumb",
        "tvbanner": "banner",
        "moviebanner": "banner",
        "hdclearart": "clearart",
        "movieart": "clearart",
        "hdmovieclearart": "clearart",
        "cdart": "cdart",
        "moviedisc": "disc",
        "seasonposter": "seasonposter",
        "seasonthumb": "seasonthumb",
        "seasonbanner": "seasonbanner",
    }

    @classmethod
    def __name(cls, fanart_name: str) -> str:
        """
        转换Fanart图片的名字为媒体服务器兼容名称。
        """
        return cls._FANART_NAME_MAP.get(fanart_name.lower(), fanart_name)

    @staticmethod
    def __image_suffix(image_url: str) -> str:
        """
        将 Fanart URL 的后缀归一为小写媒体图片后缀。

        JPEG 的两种常见写法共享 ``.jpg`` 文件名；无法从 URL 判断格式时沿用
        ``.jpg``，避免把查询参数或未知后缀写入媒体目录。

        :param image_url: Fanart 图片地址
        :return: 归一后的图片后缀
        """
        suffix = Path(urlsplit(image_url).path).suffix.lower()
        if suffix == ".jpeg":
            return ".jpg"
        if suffix in {".jpg", ".png", ".webp"}:
            return suffix
        return ".jpg"

    @classmethod
    @cached(maxsize=get_runtime_setting('CONF').fanart, ttl=get_runtime_setting('CONF').meta, shared_key="get")
    def __request_fanart(
        cls, media_type: MediaType, queryid: Union[str, int], _key_token: str = ""
    ) -> Optional[dict[str, Any]]:
        """
        查询 Fanart 图集并使用密钥指纹隔离缓存。

        :param media_type: 电影使用 movies 端点，其他类型使用 tv 端点
        :param queryid: 电影的 TMDB 标识或电视剧的 TVDB 标识
        :param _key_token: API 密钥摘要，仅参与缓存身份，不作为接口参数
        :return: 原始图集；无有效响应时为空映射，捕获请求异常时为 None
        """
        image_url = cls.__fanart_url(media_type=media_type, queryid=queryid)
        try:
            ret = RequestUtils(proxies=get_runtime_setting("PROXY"), timeout=10).get_res(
                image_url, raise_exception=True
            )
            if ret:
                payload = ret.json()
                return payload if isinstance(payload, dict) else {}
            else:
                logger.debug(f"未能获取到 {queryid} 的Fanart图片")
                return {}
        except Exception as err:
            logger.error(f"获取{queryid}的Fanart图片失败：{str(err)}")
            return None

    @classmethod
    @cached(maxsize=get_runtime_setting('CONF').fanart, ttl=get_runtime_setting('CONF').meta, shared_key="get")
    async def __async_request_fanart(
        cls, media_type: MediaType, queryid: Union[str, int], _key_token: str = ""
    ) -> Optional[dict[str, Any]]:
        """
        异步查询 Fanart 图集并使用密钥指纹隔离缓存。

        :param media_type: 电影使用 movies 端点，其他类型使用 tv 端点
        :param queryid: 电影的 TMDB 标识或电视剧的 TVDB 标识
        :param _key_token: API 密钥摘要，仅参与缓存身份，不作为接口参数
        :return: 原始图集；无有效响应时为空映射，捕获请求异常时为 None
        """
        image_url = cls.__fanart_url(media_type=media_type, queryid=queryid)
        try:
            ret = await AsyncRequestUtils(
                proxies=get_runtime_setting("PROXY"), timeout=10
            ).get_json(
                image_url
            )
            if isinstance(ret, dict):
                return ret
            logger.debug(f"未能获取到 {queryid} 的Fanart图片")
            return {}
        except Exception as err:
            logger.error(f"获取{queryid}的Fanart图片失败：{str(err)}")
            return None

    @staticmethod
    def __key_token() -> str:
        """
        返回当前 API Key 的指纹，只用于区分缓存键，不把 Key 原文写进缓存。

        Key 无效时 Fanart 返回空图集并被长期缓存；换 Key 后指纹变化，旧结果不再命中。
        """
        api_key = str(get_runtime_setting("FANART_API_KEY") or "")
        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def __fanart_url(cls, media_type: MediaType, queryid: Union[str, int]) -> str:
        """
        生成 Fanart 请求地址。
        """
        api_key = get_runtime_setting("FANART_API_KEY")
        if media_type == MediaType.MOVIE:
            return f"https://webservice.fanart.tv/v3/movies/{queryid}?api_key={api_key}"
        return f"https://webservice.fanart.tv/v3/tv/{queryid}?api_key={api_key}"

    def clear_cache(self) -> None:
        """
        清理同步缓存，并由宿主登记运行中事件循环的异步清理。
        """
        logger.info(f"开始清除{self.get_name()}缓存 ...")
        cast(CacheControls, self.__request_fanart).cache_clear()
        async_cache_clear = cast(AsyncCacheControls, self.__async_request_fanart).cache_clear()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(async_cache_clear)
        else:
            try:
                get_task_registry().create(
                    async_cache_clear,
                    owner="module.fanart.cache_clear",
                )
            except RuntimeError:
                # 关停阶段拒绝登记新任务时，同步缓存已清理，登记器负责关闭协程。
                return
        logger.info(f"{self.get_name()}缓存清除完成")
