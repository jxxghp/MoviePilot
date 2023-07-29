from ..tmdb import TMDb


class List(TMDb):
    _urls = {
        "details": "/list/%s",
        "check_status": "/list/%s/item_status",
        "create": "/list",
        "add_movie": "/list/%s/add_item",
        "remove_movie": "/list/%s/remove_item",
        "clear_list": "/list/%s/clear",
        "delete_list": "/list/%s",
    }

    def details(self, list_id):
        """
        Get list details by id.
        :param list_id: int
        :return:
        """
        return self._request_obj(self._urls["details"] % list_id, key="items")

    def check_item_status(self, list_id, movie_id):
        """
        You can use this method to check if a movie has already been added to the list.
        :param list_id: int
        :param movie_id: int
        :return:
        """
        return self._request_obj(self._urls["check_status"] % list_id, params="movie_id=%s" % movie_id)["item_present"]

    def create_list(self, name, description):
        """
        You can use this method to check if a movie has already been added to the list.
        :param name: str
        :param description: str
        :return:
        """
        return self._request_obj(
            self._urls["create"],
            params="session_id=%s" % self.session_id,
            method="POST",
            json={
                "name": name,
                "description": description,
                "language": self.language
            }
        ).list_id

    def add_movie(self, list_id, movie_id):
        """
        Add a movie to a list.
        :param list_id: int
        :param movie_id: int
        """
        self._request_obj(
            self._urls["add_movie"] % list_id,
            params="session_id=%s" % self.session_id,
            method="POST",
            json={"media_id": movie_id}
        )

    def remove_movie(self, list_id, movie_id):
        """
        Remove a movie from a list.
        :param list_id: int
        :param movie_id: int
        """
        self._request_obj(
            self._urls["remove_movie"] % list_id,
            params="session_id=%s" % self.session_id,
            method="POST",
            json={"media_id": movie_id}
        )

    def clear_list(self, list_id):
        """
        Clear all of the items from a list.
        :param list_id: int
        """
        self._request_obj(
            self._urls["clear_list"] % list_id,
            params="session_id=%s&confirm=true" % self.session_id,
            method="POST"
        )

    def delete_list(self, list_id):
        """
        Delete a list.
        :param list_id: int
        """
        self._request_obj(
            self._urls["delete_list"] % list_id,
            params="session_id=%s" % self.session_id,
            method="DELETE"
        )
