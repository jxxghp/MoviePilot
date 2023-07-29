from ..tmdb import TMDb


class Collection(TMDb):
    _urls = {
        "details": "/collection/%s",
        "images": "/collection/%s/images", 
        "translations": "/collection/%s/translations"
    }

    def details(self, collection_id):
        """
        Get collection details by id.
        :param collection_id: int
        :return:
        """
        return self._request_obj(self._urls["details"] % collection_id, key="parts")

    def images(self, collection_id):
        """
        Get the images for a collection by id.
        :param collection_id: int
        :return:
        """
        return self._request_obj(self._urls["images"] % collection_id)

    def translations(self, collection_id):
        """
        Get the list translations for a collection by id.
        :param collection_id: int
        :return:
        """
        return self._request_obj(self._urls["translations"] % collection_id, key="translations")
