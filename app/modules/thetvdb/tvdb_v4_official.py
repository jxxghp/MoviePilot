"""Official python package for using the tvdb v4 api"""

__author__ = "Weylin Wagnon"
__version__ = "1.0.12"

import json
import urllib.parse
from http import HTTPStatus

from app.utils.http import RequestUtils


class Auth:
    """
    TVDB认证类
    """

    def __init__(self, url, apikey, pin="", proxy=None, timeout: int = 15):
        login_info = {"apikey": apikey}
        if pin != "":
            login_info["pin"] = pin

        login_info_bytes = json.dumps(login_info, indent=2)

        try:
            # 使用项目统一的RequestUtils类
            req_utils = RequestUtils(proxies=proxy, timeout=timeout)
            response = req_utils.post_res(
                url=url,
                data=login_info_bytes,
                headers={"Content-Type": "application/json"}
            )

            if response and response.status_code == 200:
                result = response.json()
                self.token = result["data"]["token"]
            else:
                error_msg = f"登录失败，状态码: {response.status_code if response else 'None'}"
                if response:
                    try:
                        error_data = response.json()
                        error_msg = f"Code: {response.status_code}, {error_data.get('message', '未知错误')}"
                    except Exception as err:
                        error_msg = f"Code: {response.status_code}, 响应解析失败：{err}"
                raise Exception(error_msg)
        except Exception as e:
            raise Exception(f"TVDB认证失败: {str(e)}")

    def get_token(self):
        """
        获取认证token
        """
        return self.token


class Request:
    """
    请求处理类
    """

    def __init__(self, auth_token, proxy=None, timeout=15):
        self.auth_token = auth_token
        self.links = None
        self.proxy = proxy
        self.timeout = timeout

    def make_request(self, url, if_modified_since=None):
        """
        向指定的 URL 发起请求并返回数据
        """
        headers = {"Authorization": f"Bearer {self.auth_token}"}
        if if_modified_since:
            headers["If-Modified-Since"] = str(if_modified_since)

        try:
            # 使用项目统一的RequestUtils类
            req_utils = RequestUtils(proxies=self.proxy, timeout=self.timeout)
            response = req_utils.get_res(url=url, headers=headers)

            if response is None:
                raise ValueError(f"获取 {url} 失败\n  网络连接失败")

            if response.status_code == HTTPStatus.NOT_MODIFIED:
                return {
                    "code": HTTPStatus.NOT_MODIFIED.real,
                    "message": "Not-Modified",
                }

            if response.status_code == 200:
                result = response.json()
                data = result.get("data", None)
                if data is not None and result.get("status", "failure") != "failure":
                    self.links = result.get("links", None)
                    return data

                msg = result.get("message", "未知错误")
                raise ValueError(f"获取 {url} 失败\n  {str(msg)}")
            else:
                # 处理其他HTTP错误状态码
                try:
                    error_data = response.json()
                    msg = error_data.get("message", f"HTTP {response.status_code}")
                except Exception as err:
                    msg = f"HTTP {response.status_code} {err}"
                raise ValueError(f"获取 {url} 失败\n  {str(msg)}")

        except Exception as e:
            if isinstance(e, ValueError):
                raise
            raise ValueError(f"获取 {url} 失败\n  {str(e)}")


class Url:
    """
    URL构建类
    """

    def __init__(self):
        self.base_url = "https://api4.thetvdb.com/v4/"

    def construct(self, url_sect, url_id=None, url_subsect=None, url_lang=None, **kwargs):
        """
        构建API URL
        """
        url = self.base_url + url_sect
        if url_id:
            url += "/" + str(url_id)
        if url_subsect:
            url += "/" + url_subsect
        if url_lang:
            url += "/" + url_lang
        if kwargs:
            params = {var: val for var, val in kwargs.items() if val is not None}
            if params:
                url += "?" + urllib.parse.urlencode(params)
        return url


class TVDB:
    """
    TVDB API主类
    """

    def __init__(self, apikey: str, pin="", proxy=None, timeout: int = 15):
        self.url = Url()
        login_url = self.url.construct("login")
        self.auth = Auth(login_url, apikey, pin, proxy, timeout)
        auth_token = self.auth.get_token()
        self.request = Request(auth_token, proxy, timeout)

    def get_req_links(self) -> dict:
        """
        获取上一次请求返回的链接信息（例如分页链接）
        """
        return self.request.links

    def get_artwork_statuses(self, meta=None, if_modified_since=None) -> list:
        """
        返回艺术图状态列表
        """
        url = self.url.construct("artwork/statuses", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_artwork_types(self, meta=None, if_modified_since=None) -> list:
        """
        返回艺术图类型列表
        """
        url = self.url.construct("artwork/types", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_artwork(self, id: int, meta=None, if_modified_since=None) -> dict:
        """
        返回单个艺术图信息的字典
        """
        url = self.url.construct("artwork", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_artwork_extended(self, id: int, meta=None, if_modified_since=None) -> dict:
        """
        返回单个艺术图的扩展信息字典
        """
        url = self.url.construct("artwork", id, "extended", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_all_awards(self, meta=None, if_modified_since=None) -> list:
        """
        返回奖项列表
        """
        url = self.url.construct("awards", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_award(self, id: int, meta=None, if_modified_since=None) -> dict:
        """
        返回单个奖项信息的字典
        """
        url = self.url.construct("awards", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_award_extended(self, id: int, meta=None, if_modified_since=None) -> dict:
        """
        返回单个奖项的扩展信息字典
        """
        url = self.url.construct("awards", id, "extended", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_all_award_categories(self, meta=None, if_modified_since=None) -> list:
        """
        返回奖项类别列表
        """
        url = self.url.construct("awards/categories", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_award_category(self, id: int, meta=None, if_modified_since=None) -> dict:
        """
        返回单个奖项类别信息的字典
        """
        url = self.url.construct("awards/categories", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_award_category_extended(self, id: int, meta=None, if_modified_since=None) -> dict:
        """
        返回单个奖项类别的扩展信息字典
        """
        url = self.url.construct("awards/categories", id, "extended", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_content_ratings(self, meta=None, if_modified_since=None) -> list:
        """
        返回内容分级列表
        """
        url = self.url.construct("content/ratings", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_countries(self, meta=None, if_modified_since=None) -> list:
        """
        返回国家列表
        """
        url = self.url.construct("countries", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_all_companies(self, page=None, meta=None, if_modified_since=None) -> list:
        """
        返回公司列表 (可分页)
        """
        url = self.url.construct("companies", page=page, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_company_types(self, meta=None, if_modified_since=None) -> list:
        """
        返回公司类型列表
        """
        url = self.url.construct("companies/types", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_company(self, id: int, meta=None, if_modified_since=None) -> dict:
        """
        返回单个公司信息的字典
        """
        url = self.url.construct("companies", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_all_series(self, page=None, meta=None, if_modified_since=None) -> list:
        """
        返回剧集列表 (可分页)
        """
        url = self.url.construct("series", page=page, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_series(self, id: int, meta=None, if_modified_since=None) -> dict:
        """
        返回单个剧集信息的字典
        """
        url = self.url.construct("series", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_series_by_slug(self, slug: str, meta=None, if_modified_since=None) -> dict:
        """
        通过 slug (别名) 返回单个剧集信息的字典
        """
        url = self.url.construct("series/slug", slug, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_series_extended(self, id: int, meta=None, short=False, if_modified_since=None) -> dict:
        """
        返回单个剧集的扩展信息字典
        """
        url = self.url.construct("series", id, "extended", meta=meta, short=short)
        return self.request.make_request(url, if_modified_since)

    def get_series_episodes(self, id: int, season_type: str = "default", page: int = 0,
                            lang: str = None, meta=None, if_modified_since=None, **kwargs) -> dict:
        """
        返回指定剧集和季类型的各集信息字典 (可分页，可指定语言)
        """
        url = self.url.construct(
            "series", id, "episodes/" + season_type, lang, page=page, meta=meta, **kwargs
        )
        return self.request.make_request(url, if_modified_since)

    def get_series_translation(self, id: int, lang: str, meta=None, if_modified_since=None) -> dict:
        """
        返回剧集的指定语言翻译信息字典
        """
        url = self.url.construct("series", id, "translations", lang, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_series_artworks(self, id: int, lang: str, type=None, if_modified_since=None) -> dict:
        """
        返回包含艺术图数组的剧集记录 (可指定语言和类型)
        """
        url = self.url.construct("series", id, "artworks", lang=lang, type=type)
        return self.request.make_request(url, if_modified_since)

    def get_series_next_aired(self, id: int, if_modified_since=None) -> dict:
        """
        返回剧集的下一播出信息字典
        """
        url = self.url.construct("series", id, "nextAired")
        return self.request.make_request(url, if_modified_since)

    def get_all_movies(self, page=None, meta=None, if_modified_since=None) -> list:
        """
        返回电影列表 (可分页)
        """
        url = self.url.construct("movies", page=page, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_movie(self, id: int, meta=None, if_modified_since=None) -> dict:
        """
        返回单个电影信息的字典
        """
        url = self.url.construct("movies", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_movie_by_slug(self, slug: str, meta=None, if_modified_since=None) -> dict:
        """
        通过 slug (别名) 返回单个电影信息的字典
        """
        url = self.url.construct("movies/slug", slug, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_movie_extended(self, id: int, meta=None, short=False, if_modified_since=None) -> dict:
        """
        返回电影的扩展信息字典
        """
        url = self.url.construct("movies", id, "extended", meta=meta, short=short)
        return self.request.make_request(url, if_modified_since)

    def get_movie_translation(self, id: int, lang: str, meta=None, if_modified_since=None) -> dict:
        """
        返回电影的指定语言翻译信息字典
        """
        url = self.url.construct("movies", id, "translations", lang, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_all_seasons(self, page=None, meta=None, if_modified_since=None) -> list:
        """
        返回季列表 (可分页)
        """
        url = self.url.construct("seasons", page=page, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_season(self, id: int, meta=None, if_modified_since=None) -> dict:
        """
        返回单季信息的字典
        """
        url = self.url.construct("seasons", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_season_extended(self, id: int, meta=None, if_modified_since=None) -> dict:
        """
        返回单季的扩展信息字典
        """
        url = self.url.construct("seasons", id, "extended", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_season_types(self, meta=None, if_modified_since=None) -> list:
        """
        返回季类型列表
        """
        url = self.url.construct("seasons/types", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_season_translation(self, id: int, lang: str, meta=None, if_modified_since=None) -> dict:
        """
        返回季的指定语言翻译信息字典
        """
        url = self.url.construct("seasons", id, "translations", lang, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_all_episodes(self, page=None, meta=None, if_modified_since=None) -> list:
        """
        返回集列表 (可分页)
        """
        url = self.url.construct("episodes", page=page, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_episode(self, id: int, meta=None, if_modified_since=None) -> dict:
        """
        返回单集信息的字典
        """
        url = self.url.construct("episodes", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_episode_extended(self, id: int, meta=None, if_modified_since=None) -> dict:
        """
        返回单集的扩展信息字典
        """
        url = self.url.construct("episodes", id, "extended", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_episode_translation(self, id: int, lang: str, meta=None, if_modified_since=None) -> dict:
        """
        返回单集的指定语言翻译信息字典
        """
        url = self.url.construct("episodes", id, "translations", lang, meta=meta)
        return self.request.make_request(url, if_modified_since)

    # 兼容旧函数名。
    get_episodes_translation = get_episode_translation

    def get_all_genders(self, meta=None, if_modified_since=None) -> list:
        """
        返回性别列表
        """
        url = self.url.construct("genders", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_all_genres(self, meta=None, if_modified_since=None) -> list:
        """
        返回类型（流派）列表
        """
        url = self.url.construct("genres", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_genre(self, id: int, meta=None, if_modified_since=None) -> dict:
        """
        返回单个类型（流派）信息的字典
        """
        url = self.url.construct("genres", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_all_languages(self, meta=None, if_modified_since=None) -> list:
        """
        返回语言列表
        """
        url = self.url.construct("languages", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_all_people(self, page=None, meta=None, if_modified_since=None) -> list:
        """
        返回人物列表 (可分页)
        """
        url = self.url.construct("people", page=page, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_person(self, id: int, meta=None, if_modified_since=None) -> dict:
        """
        返回单个人物信息的字典
        """
        url = self.url.construct("people", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_person_extended(self, id: int, meta=None, if_modified_since=None) -> dict:
        """
        返回单个人物的扩展信息字典
        """
        url = self.url.construct("people", id, "extended", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_person_translation(self, id: int, lang: str, meta=None, if_modified_since=None) -> dict:
        """
        返回人物的指定语言翻译信息字典
        """
        url = self.url.construct("people", id, "translations", lang, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_character(self, id: int, meta=None, if_modified_since=None) -> dict:
        """
        返回角色信息的字典
        """
        url = self.url.construct("characters", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_people_types(self, meta=None, if_modified_since=None) -> list:
        """
        返回人物类型列表
        """
        url = self.url.construct("people/types", meta=meta)
        return self.request.make_request(url, if_modified_since)

    # 兼容旧函数名
    get_all_people_types = get_people_types

    def get_source_types(self, meta=None, if_modified_since=None) -> list:
        """
        返回来源类型列表
        """
        url = self.url.construct("sources/types", meta=meta)
        return self.request.make_request(url, if_modified_since)

    # 兼容旧函数名
    get_all_sourcetypes = get_source_types

    def get_updates(self, since: int, **kwargs) -> list:
        """
        返回更新列表
        """
        url = self.url.construct("updates", since=since, **kwargs)
        return self.request.make_request(url)

    def get_all_tag_options(self, page=None, meta=None, if_modified_since=None) -> list:
        """
        返回标签选项列表 (可分页)
        """
        url = self.url.construct("tags/options", page=page, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_tag_option(self, id: int, meta=None, if_modified_since=None) -> dict:
        """
        返回单个标签选项信息的字典
        """
        url = self.url.construct("tags/options", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_all_lists(self, page=None, meta=None) -> dict:
        """
        返回所有公开的列表信息 (可分页)
        """
        url = self.url.construct("lists", page=page, meta=meta)
        return self.request.make_request(url)

    def get_list(self, id: int, meta=None, if_modified_since=None) -> dict:
        """
        返回单个列表信息的字典
        """
        url = self.url.construct("lists", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_list_by_slug(self, slug: str, meta=None, if_modified_since=None) -> dict:
        """
        通过 slug (别名) 返回单个列表信息的字典
        """
        url = self.url.construct("lists/slug", slug, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_list_extended(self, id: int, meta=None, if_modified_since=None) -> dict:
        """
        返回单个列表的扩展信息字典
        """
        url = self.url.construct("lists", id, "extended", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_list_translation(self, id: int, lang: str, meta=None, if_modified_since=None) -> dict:
        """
        返回列表的指定语言翻译信息字典
        """
        url = self.url.construct("lists", id, "translations", lang, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_inspiration_types(self, meta=None, if_modified_since=None) -> dict:
        """
        返回灵感类型列表
        """
        url = self.url.construct("inspiration/types", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def search(self, query: str, **kwargs) -> list:
        """
        根据查询字符串进行搜索，并返回结果列表
        """
        url = self.url.construct("search", query=query, **kwargs)
        return self.request.make_request(url)

    def search_by_remote_id(self, remoteid: str) -> list:
        """
        通过外部 ID 精确匹配搜索，并返回结果列表
        """
        url = self.url.construct("search/remoteid", remoteid)
        return self.request.make_request(url)

    def get_tags(self, slug: str, if_modified_since=None) -> dict:
        """
        返回具有指定 slug 的标签实体信息字典 (此方法基于的 /entities/{slug} 端点非标准，请谨慎使用)
        """
        url = self.url.construct("entities", url_subsect=slug)
        return self.request.make_request(url, if_modified_since)

    def get_entities_types(self, if_modified_since=None) -> dict:
        """
        返回可用的实体类型列表
        """
        url = self.url.construct("entities")
        return self.request.make_request(url, if_modified_since)

    def get_user_by_id(self, id: int) -> dict:
        """
        通过用户 ID 返回用户信息字典
        """
        url = self.url.construct("user", id)
        return self.request.make_request(url)

    def get_user(self) -> dict:
        """
        返回当前认证的用户信息字典
        """
        url = self.url.construct("user")
        return self.request.make_request(url)

    def get_user_favorites(self) -> dict:
        """
        返回当前认证用户的收藏夹信息字典
        """
        url = self.url.construct('user/favorites')
        return self.request.make_request(url)
