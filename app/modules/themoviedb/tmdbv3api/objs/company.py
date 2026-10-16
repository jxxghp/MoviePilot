from ..tmdb import TMDb, TMDbJson


class Company(TMDb):
    """提供公司详情、别名、图像和电影分页查询。"""

    _urls = {
        "details": "/company/%s",
        "alternative_names": "/company/%s/alternative_names",
        "images": "/company/%s/images",
        "movies": "/company/%s/movies"
    }

    def details(self, company_id: int | str) -> TMDbJson:
        """
        Get a companies details by id.
        :param company_id: int
        :return:
        """
        return self._request_obj(self._urls["details"] % company_id)

    def alternative_names(self, company_id: int | str) -> TMDbJson:
        """
        Get the alternative names of a company.
        :param company_id: int
        :return:
        """
        return self._request_obj(self._urls["alternative_names"] % company_id, key="results")

    def images(self, company_id: int | str) -> TMDbJson:
        """
        Get the alternative names of a company.
        :param company_id: int
        :return:
        """
        return self._request_obj(self._urls["images"] % company_id, key="logos")

    def movies(self, company_id: int | str, page: int=1) -> TMDbJson:
        """
        Get the movies of a company by id.
        :param company_id: int
        :param page: int
        :return:
        """
        return self._request_obj(
            self._urls["movies"] % company_id,
            params="page=%s" % page,
            key="results"
        )

    # 异步版本方法
    async def async_details(self, company_id: int | str) -> TMDbJson:
        """
        Get a companies details by id.（异步版本）
        :param company_id: int
        :return:
        """
        return await self._async_request_obj(self._urls["details"] % company_id)

    async def async_alternative_names(self, company_id: int | str) -> TMDbJson:
        """
        Get the alternative names of a company.（异步版本）
        :param company_id: int
        :return:
        """
        return await self._async_request_obj(self._urls["alternative_names"] % company_id, key="results")

    async def async_images(self, company_id: int | str) -> TMDbJson:
        """
        Get the alternative names of a company.（异步版本）
        :param company_id: int
        :return:
        """
        return await self._async_request_obj(self._urls["images"] % company_id, key="logos")

    async def async_movies(self, company_id: int | str, page: int=1) -> TMDbJson:
        """
        Get the movies of a company by id.（异步版本）
        :param company_id: int
        :param page: int
        :return:
        """
        return await self._async_request_obj(
            self._urls["movies"] % company_id,
            params="page=%s" % page,
            key="results"
        )
