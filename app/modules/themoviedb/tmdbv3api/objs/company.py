from ..tmdb import TMDb


class Company(TMDb):
    _urls = {
        "details": "/company/%s", 
        "alternative_names": "/company/%s/alternative_names",
        "images": "/company/%s/images",
        "movies": "/company/%s/movies"
    }

    def details(self, company_id):
        """
        Get a companies details by id.
        :param company_id: int
        :return:
        """
        return self._request_obj(self._urls["details"] % company_id)

    def alternative_names(self, company_id):
        """
        Get the alternative names of a company.
        :param company_id: int
        :return:
        """
        return self._request_obj(self._urls["alternative_names"] % company_id, key="results")

    def images(self, company_id):
        """
        Get the alternative names of a company.
        :param company_id: int
        :return:
        """
        return self._request_obj(self._urls["images"] % company_id, key="logos")

    def movies(self, company_id, page=1):
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
