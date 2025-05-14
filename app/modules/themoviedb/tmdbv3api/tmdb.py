# -*- coding: utf-8 -*-

import logging
import time
from datetime import datetime

import requests
import requests.exceptions

from app.core.cache import cached
from app.core.config import settings
from app.utils.http import RequestUtils
from .exceptions import TMDbException

logger = logging.getLogger(__name__)


class TMDb(object):
    _req = None
    _session = None

    def __init__(self, obj_cached=True, session=None, language=None):
        self._api_key = settings.TMDB_API_KEY
        self._language = language or settings.TMDB_LOCALE or "en-US"
        self._session_id = None
        self._wait_on_rate_limit = True
        self._debug_enabled = False
        self._cache_enabled = obj_cached
        self._proxies = settings.PROXY
        self._domain = settings.TMDB_API_DOMAIN
        self._page = None
        self._total_results = None
        self._total_pages = None

        if session is not None:
            self._req = RequestUtils(session=session, proxies=self.proxies)
        else:
            self._session = requests.Session()
            self._req = RequestUtils(session=self._session, proxies=self.proxies)
        self._remaining = 40
        self._reset = None
        self._timeout = 15
        self.obj_cached = obj_cached

    @property
    def page(self):
        return self._page

    @property
    def total_results(self):
        return self._total_results

    @property
    def total_pages(self):
        return self._total_pages

    @property
    def api_key(self):
        return self._api_key

    @property
    def domain(self):
        return self._domain

    @property
    def proxies(self):
        return self._proxies

    @proxies.setter
    def proxies(self, proxies):
        self._proxies = proxies

    @api_key.setter
    def api_key(self, api_key):
        self._api_key = str(api_key)

    @domain.setter
    def domain(self, domain):
        self._domain = str(domain)

    @property
    def language(self):
        return self._language

    @language.setter
    def language(self, language):
        self._language = language

    @property
    def has_session(self):
        return True if self._session_id else False

    @property
    def session_id(self):
        if not self._session_id:
            raise TMDbException("Must Authenticate to create a session run Authentication(username, password)")
        return self._session_id

    @session_id.setter
    def session_id(self, session_id):
        self._session_id = session_id

    @property
    def wait_on_rate_limit(self):
        return self._wait_on_rate_limit

    @wait_on_rate_limit.setter
    def wait_on_rate_limit(self, wait_on_rate_limit):
        self._wait_on_rate_limit = bool(wait_on_rate_limit)

    @property
    def debug(self):
        return self._debug_enabled

    @debug.setter
    def debug(self, debug):
        self._debug_enabled = bool(debug)

    @property
    def cache(self):
        return self._cache_enabled

    @cache.setter
    def cache(self, cache):
        self._cache_enabled = bool(cache)

    @cached(maxsize=settings.CACHE_CONF["tmdb"], ttl=settings.CACHE_CONF["meta"])
    def cached_request(self, method, url, data, json,
                       _ts=datetime.strftime(datetime.now(), '%Y%m%d')):
        """
        缓存请求
        """
        return self.request(method, url, data, json)

    def request(self, method, url, data, json):
        if method == "GET":
            req = self._req.get_res(url, params=data, json=json)
        else:
            req = self._req.post_res(url, data=data, json=json)
        if req is None:
            raise TMDbException("无法连接TheMovieDb，请检查网络连接！")
        return req

    def cache_clear(self):
        return self.cached_request.cache_clear()

    def _request_obj(self, action, params="", call_cached=True,
                     method="GET", data=None, json=None, key=None):
        if self.api_key is None or self.api_key == "":
            raise TMDbException("TheMovieDb API Key 未设置！")

        url = "https://%s/3%s?api_key=%s&%s&language=%s" % (
            self.domain,
            action,
            self.api_key,
            params,
            self.language,
        )

        if self.cache and self.obj_cached and call_cached and method != "POST":
            req = self.cached_request(method, url, data, json)
        else:
            req = self.request(method, url, data, json)

        if req is None:
            return None

        headers = req.headers

        if "X-RateLimit-Remaining" in headers:
            self._remaining = int(headers["X-RateLimit-Remaining"])

        if "X-RateLimit-Reset" in headers:
            self._reset = int(headers["X-RateLimit-Reset"])

        if self._remaining < 1:
            current_time = int(time.time())
            sleep_time = self._reset - current_time

            if self.wait_on_rate_limit:
                logger.warning("达到请求频率限制，休眠：%d 秒..." % sleep_time)
                time.sleep(abs(sleep_time))
                return self._request_obj(action, params, call_cached, method, data, json, key)
            else:
                raise TMDbException("达到请求频率限制，将在 %d 秒后重试..." % sleep_time)

        json_data = req.json()

        if "page" in json_data:
            self._page = json_data["page"]

        if "total_results" in json_data:
            self._total_results = json_data["total_results"]

        if "total_pages" in json_data:
            self._total_pages = json_data["total_pages"]

        if self.debug:
            logger.info(json_data)
            logger.info(self.cached_request.cache_info())

        if "errors" in json_data:
            raise TMDbException(json_data["errors"])

        if "success" in json_data and json_data["success"] is False:
            raise TMDbException(json_data["status_message"])

        if key:
            return json_data.get(key)
        return json_data

    def close(self):
        if self._session:
            self._session.close()
