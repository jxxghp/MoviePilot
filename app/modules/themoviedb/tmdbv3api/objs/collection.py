"""
TMDB 电影合集接口的同步与异步调用封装。
"""

from typing import cast

from ..tmdb import TMDb, TMDbJson


class Collection(TMDb):
    """
    提供 TMDB 电影合集接口。

    网络、限流和业务响应处理沿用 TMDb 基类；接口字段保持原始 JSON 结构，
    不在此层转换为 MoviePilot 领域模型。
    """
    _urls = {
        "details": "/collection/%s",
        "images": "/collection/%s/images",
        "translations": "/collection/%s/translations"
    }

    def details(self, collection_id: int | str) -> list[dict[str, TMDbJson]]:
        """
        查询电影合集详情。

        :param collection_id: 目标电影合集的 TMDB 标识
        :return: 响应的 parts 字段原值；未取得响应或字段缺失时返回 None
        """
        return cast(
            list[dict[str, TMDbJson]],
            self._request_obj(self._urls["details"] % collection_id, key="parts"),
        )

    def images(self, collection_id: int | str) -> TMDbJson:
        """
        查询电影合集图片信息。

        :param collection_id: 目标电影合集的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return self._request_obj(self._urls["images"] % collection_id)

    def translations(self, collection_id: int | str) -> TMDbJson:
        """
        查询电影合集多语言翻译内容。

        :param collection_id: 目标电影合集的 TMDB 标识
        :return: 响应的 translations 字段原值；未取得响应或字段缺失时返回 None
        """
        return self._request_obj(self._urls["translations"] % collection_id, key="translations")

    async def async_details(
        self,
        collection_id: int | str,
    ) -> list[dict[str, TMDbJson]]:
        """
        查询电影合集详情。

        :param collection_id: 目标电影合集的 TMDB 标识
        :return: 响应的 parts 字段原值；未取得响应或字段缺失时返回 None
        """
        return cast(
            list[dict[str, TMDbJson]],
            await self._async_request_obj(
                self._urls["details"] % collection_id,
                key="parts",
            ),
        )

    async def async_images(self, collection_id: int | str) -> TMDbJson:
        """
        查询电影合集图片信息。

        :param collection_id: 目标电影合集的 TMDB 标识
        :return: 接口原始 JSON 对象或数组；未取得响应时返回 None
        """
        return await self._async_request_obj(self._urls["images"] % collection_id)

    async def async_translations(self, collection_id: int | str) -> TMDbJson:
        """
        查询电影合集多语言翻译内容。

        :param collection_id: 目标电影合集的 TMDB 标识
        :return: 响应的 translations 字段原值；未取得响应或字段缺失时返回 None
        """
        return await self._async_request_obj(self._urls["translations"] % collection_id, key="translations")
