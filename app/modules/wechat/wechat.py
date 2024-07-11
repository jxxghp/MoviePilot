import json
import re
import threading
from datetime import datetime
from typing import Optional, List, Dict

from app.core.config import settings
from app.core.context import MediaInfo, Context
from app.core.metainfo import MetaInfo
from app.log import logger
from app.utils.common import retry
from app.utils.http import RequestUtils
from app.utils.string import StringUtils

lock = threading.Lock()


class WeChat:
    # 企业微信Token
    _access_token = None
    # 企业微信Token过期时间
    _expires_in: int = None
    # 企业微信Token获取时间
    _access_token_time: datetime = None
    # 企业微信CorpID
    _corpid = None
    # 企业微信AppSecret
    _appsecret = None
    # 企业微信AppID
    _appid = None

    # 企业微信发送消息URL
    _send_msg_url = f"{settings.WECHAT_PROXY}/cgi-bin/message/send?access_token=%s"
    # 企业微信获取TokenURL
    _token_url = f"{settings.WECHAT_PROXY}/cgi-bin/gettoken?corpid=%s&corpsecret=%s"
    # 企业微信创新菜单URL
    _create_menu_url = f"{settings.WECHAT_PROXY}/cgi-bin/menu/create?access_token=%s&agentid=%s"

    def __init__(self):
        """
        初始化
        """
        self._corpid = settings.WECHAT_CORPID
        self._appsecret = settings.WECHAT_APP_SECRET
        self._appid = settings.WECHAT_APP_ID

        if self._corpid and self._appsecret and self._appid:
            self.__get_access_token()

    def get_state(self):
        """
        获取状态
        """
        return True if self.__get_access_token else False

    @retry(Exception, logger=logger)
    def __get_access_token(self, force=False):
        """
        获取微信Token
        :return： 微信Token
        """
        token_flag = True
        if not self._access_token:
            token_flag = False
        else:
            if (datetime.now() - self._access_token_time).seconds >= self._expires_in:
                token_flag = False

        if not token_flag or force:
            if not self._corpid or not self._appsecret:
                return None
            try:
                token_url = self._token_url % (self._corpid, self._appsecret)
                res = RequestUtils().get_res(token_url)
                if res:
                    ret_json = res.json()
                    if ret_json.get('errcode') == 0:
                        self._access_token = ret_json.get('access_token')
                        self._expires_in = ret_json.get('expires_in')
                        self._access_token_time = datetime.now()
                elif res is not None:
                    logger.error(f"获取微信access_token失败，错误码：{res.status_code}，错误原因：{res.reason}")
                else:
                    logger.error(f"获取微信access_token失败，未获取到返回信息")
                    raise Exception("获取微信access_token失败，网络连接失败")
            except Exception as e:
                logger.error(f"获取微信access_token失败，错误信息：{str(e)}")
                return None
        return self._access_token

    def __send_message(self, title: str, text: str = None,
                       userid: str = None, link: str = None) -> Optional[bool]:
        """
        发送文本消息
        :param title: 消息标题
        :param text: 消息内容
        :param userid: 消息发送对象的ID，为空则发给所有人
        :param link: 跳转链接
        :return: 发送状态，错误信息
        """
        message_url = self._send_msg_url % self.__get_access_token()
        if text:
            content = "%s\n%s" % (title, text.replace("\n\n", "\n"))
        else:
            content = title

        if link:
            content = f"{content}\n点击查看：{link}"

        if not userid:
            userid = "@all"

        # Check if content exceeds 2048 bytes and split if necessary
        if len(content.encode('utf-8')) > 2048:
            content_chunks = []
            current_chunk = ""
            for line in content.splitlines():
                if len(current_chunk.encode('utf-8')) + len(line.encode('utf-8')) > 2048:
                    content_chunks.append(current_chunk.strip())
                    current_chunk = ""
                current_chunk += line + "\n"
            if current_chunk:
                content_chunks.append(current_chunk.strip())

            # Send each chunk as a separate message
            for chunk in content_chunks:
                req_json = {
                    "touser": userid,
                    "msgtype": "text",
                    "agentid": self._appid,
                    "text": {
                        "content": chunk
                    },
                    "safe": 0,
                    "enable_id_trans": 0,
                    "enable_duplicate_check": 0
                }
                result = self.__post_request(message_url, req_json)
        else:
            req_json = {
                "touser": userid,
                "msgtype": "text",
                "agentid": self._appid,
                "text": {
                    "content": content
                },
                "safe": 0,
                "enable_id_trans": 0,
                "enable_duplicate_check": 0
            }
            return self.__post_request(message_url, req_json)

        return result

    def __send_image_message(self, title: str, text: str, image_url: str,
                             userid: str = None, link: str = None) -> Optional[bool]:
        """
        发送图文消息
        :param title: 消息标题
        :param text: 消息内容
        :param image_url: 图片地址
        :param userid: 消息发送对象的ID，为空则发给所有人
        :param link: 跳转链接
        :return: 发送状态，错误信息
        """
        message_url = self._send_msg_url % self.__get_access_token()
        if text:
            text = text.replace("\n\n", "\n")
        if not userid:
            userid = "@all"
        req_json = {
            "touser": userid,
            "msgtype": "news",
            "agentid": self._appid,
            "news": {
                "articles": [
                    {
                        "title": title,
                        "description": text,
                        "picurl": image_url,
                        "url": link
                    }
                ]
            }
        }
        return self.__post_request(message_url, req_json)

    def send_msg(self, title: str, text: str = "", image: str = "",
                 userid: str = None, link: str = None) -> Optional[bool]:
        """
        微信消息发送入口，支持文本、图片、链接跳转、指定发送对象
        :param title: 消息标题
        :param text: 消息内容
        :param image: 图片地址
        :param userid: 消息发送对象的ID，为空则发给所有人
        :param link: 跳转链接
        :return: 发送状态，错误信息
        """
        if not self.__get_access_token():
            logger.error("获取微信access_token失败，请检查参数配置")
            return None

        if image:
            ret_code = self.__send_image_message(title=title, text=text, image_url=image, userid=userid, link=link)
        else:
            ret_code = self.__send_message(title=title, text=text, userid=userid, link=link)

        return ret_code

    def send_medias_msg(self, medias: List[MediaInfo], userid: str = "") -> Optional[bool]:
        """
        发送列表类消息
        """
        if not self.__get_access_token():
            logger.error("获取微信access_token失败，请检查参数配置")
            return None

        message_url = self._send_msg_url % self.__get_access_token()
        if not userid:
            userid = "@all"
        articles = []
        index = 1
        for media in medias:
            if media.vote_average:
                title = f"{index}. {media.title_year}\n类型：{media.type.value}，评分：{media.vote_average}"
            else:
                title = f"{index}. {media.title_year}\n类型：{media.type.value}"
            articles.append({
                "title": title,
                "description": "",
                "picurl": media.get_message_image() if index == 1 else media.get_poster_image(),
                "url": media.detail_link
            })
            index += 1

        req_json = {
            "touser": userid,
            "msgtype": "news",
            "agentid": self._appid,
            "news": {
                "articles": articles
            }
        }
        return self.__post_request(message_url, req_json)

    def send_torrents_msg(self, torrents: List[Context],
                          userid: str = "", title: str = "", link: str = None) -> Optional[bool]:
        """
        发送列表消息
        """
        if not self.__get_access_token():
            logger.error("获取微信access_token失败，请检查参数配置")
            return None

        # 先发送标题
        if title:
            self.__send_message(title=title, userid=userid, link=link)

        # 发送列表
        message_url = self._send_msg_url % self.__get_access_token()
        if not userid:
            userid = "@all"
        articles = []
        index = 1
        for context in torrents:
            torrent = context.torrent_info
            meta = MetaInfo(title=torrent.title, subtitle=torrent.description)
            mediainfo = context.media_info
            torrent_title = f"{index}.【{torrent.site_name}】" \
                            f"{meta.season_episode} " \
                            f"{meta.resource_term} " \
                            f"{meta.video_term} " \
                            f"{meta.release_group} " \
                            f"{StringUtils.str_filesize(torrent.size)} " \
                            f"{torrent.volume_factor} " \
                            f"{torrent.seeders}↑"
            title = re.sub(r"\s+", " ", title).strip()
            articles.append({
                "title": torrent_title,
                "description": torrent.description if index == 1 else '',
                "picurl": mediainfo.get_message_image() if index == 1 else '',
                "url": torrent.page_url
            })
            index += 1

        req_json = {
            "touser": userid,
            "msgtype": "news",
            "agentid": self._appid,
            "news": {
                "articles": articles
            }
        }
        return self.__post_request(message_url, req_json)

    def __post_request(self, message_url: str, req_json: dict) -> bool:
        """
        向微信发送请求
        """
        try:
            res = RequestUtils(content_type='application/json').post(
                message_url,
                data=json.dumps(req_json, ensure_ascii=False).encode('utf-8')
            )
            if res and res.status_code == 200:
                ret_json = res.json()
                if ret_json.get('errcode') == 0:
                    return True
                else:
                    if ret_json.get('errcode') == 42001:
                        self.__get_access_token(force=True)
                    logger.error(f"发送请求失败，错误信息：{ret_json.get('errmsg')}")
                    return False
            elif res is not None:
                logger.error(f"发送请求失败，错误码：{res.status_code}，错误原因：{res.reason}")
                return False
            else:
                logger.error(f"发送请求失败，未获取到返回信息")
                return False
        except Exception as err:
            logger.error(f"发送请求失败，错误信息：{str(err)}")
            return False

    def create_menus(self, commands: Dict[str, dict]):
        """
        自动注册微信菜单
        :param commands: 命令字典
        命令字典：
        {
            "/cookiecloud": {
                "func": CookieCloudChain(self._db).remote_sync,
                "description": "同步站点",
                "category": "站点",
                "data": {}
            }
        }
        注册报文格式，一级菜单只有最多3条，子菜单最多只有5条：
        {
           "button":[
               {
                   "type":"click",
                   "name":"今日歌曲",
                   "key":"V1001_TODAY_MUSIC"
               },
               {
                   "name":"菜单",
                   "sub_button":[
                       {
                           "type":"view",
                           "name":"搜索",
                           "url":"http://www.soso.com/"
                       },
                       {
                           "type":"click",
                           "name":"赞一下我们",
                           "key":"V1001_GOOD"
                       }
                   ]
              }
           ]
        }
        """
        # 请求URL
        req_url = self._create_menu_url % (self.__get_access_token(), self._appid)

        # 对commands按category分组
        category_dict = {}
        for key, value in commands.items():
            category: Dict[str, dict] = value.get("category")
            if category:
                if not category_dict.get(category):
                    category_dict[category] = {}
                category_dict[category][key] = value

        # 一级菜单
        buttons = []
        for category, menu in category_dict.items():
            # 二级菜单
            sub_buttons = []
            for key, value in menu.items():
                sub_buttons.append({
                    "type": "click",
                    "name": value.get("description"),
                    "key": key
                })
            buttons.append({
                "name": category,
                "sub_button": sub_buttons[:5]
            })

        if buttons:
            # 发送请求
            self.__post_request(req_url, {
                "button": buttons[:3]
            })
