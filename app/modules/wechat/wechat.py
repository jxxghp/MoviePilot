import json
import threading
from datetime import datetime
from typing import Optional, List

from app.core.config import settings
from app.core.context import MediaInfo, Context
from app.log import logger
from app.utils.http import RequestUtils
from app.utils.singleton import Singleton

lock = threading.Lock()


class WeChat(metaclass=Singleton):
    
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
    _send_msg_url = "https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=%s"
    # 企业微信获取TokenURL
    _token_url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=%s&corpsecret=%s"

    def __init__(self):
        """
        初始化
        """
        self._corpid = settings.WECHAT_CORPID
        self._appsecret = settings.WECHAT_APP_SECRET
        self._appid = settings.WECHAT_APP_ID

        if self._corpid and self._appsecret and self._appid:
            self.__get_access_token()

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
            except Exception as e:
                logger.error(f"获取微信access_token失败，错误信息：{e}")
                return None
        return self._access_token

    def __send_message(self, title: str, text: str, userid: str = None) -> Optional[bool]:
        """
        发送文本消息
        :param title: 消息标题
        :param text: 消息内容
        :param userid: 消息发送对象的ID，为空则发给所有人
        :return: 发送状态，错误信息
        """
        message_url = self._send_msg_url % self.__get_access_token()
        if text:
            conent = "%s\n%s" % (title, text.replace("\n\n", "\n"))
        else:
            conent = title

        if not userid:
            userid = "@all"
        req_json = {
            "touser": userid,
            "msgtype": "text",
            "agentid": self._appid,
            "text": {
                "content": conent
            },
            "safe": 0,
            "enable_id_trans": 0,
            "enable_duplicate_check": 0
        }
        return self.__post_request(message_url, req_json)

    def __send_image_message(self, title: str, text: str, image_url: str, userid: str = None) -> Optional[bool]:
        """
        发送图文消息
        :param title: 消息标题
        :param text: 消息内容
        :param image_url: 图片地址
        :param userid: 消息发送对象的ID，为空则发给所有人
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
                        "url": ''
                    }
                ]
            }
        }
        return self.__post_request(message_url, req_json)

    def send_msg(self, title: str, text: str = "", image: str = "", userid: str = None) -> Optional[bool]:
        """
        微信消息发送入口，支持文本、图片、链接跳转、指定发送对象
        :param title: 消息标题
        :param text: 消息内容
        :param image: 图片地址
        :param userid: 消息发送对象的ID，为空则发给所有人
        :return: 发送状态，错误信息
        """
        if not self.__get_access_token():
            logger.error("获取微信access_token失败，请检查参数配置")
            return None

        if image:
            ret_code = self.__send_image_message(title, text, image, userid)
        else:
            ret_code = self.__send_message(title, text, userid)

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
                title = f"{index}. {media.get_title_string()}\n类型：{media.type.value}，评分：{media.vote_average}"
            else:
                title = f"{index}. {media.get_title_string()}\n类型：{media.type.value}"
            articles.append({
                "title": title,
                "description": "",
                "picurl": media.get_message_image() if index == 1 else media.get_poster_image(),
                "url": media.get_detail_url()
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

    def send_torrents_msg(self, torrents: List[Context], userid: str = "", title: str = "") -> Optional[bool]:
        """
        发送列表消息
        """
        if not self.__get_access_token():
            logger.error("获取微信access_token失败，请检查参数配置")
            return None

        try:
            index, caption = 1, "*%s*" % title
            for context in torrents:
                torrent = context.torrent_info
                link = torrent.page_url
                title = torrent.title
                free = torrent.get_volume_factor_string()
                seeder = f"{torrent.seeders}↑"
                description = torrent.description
                caption = f"{caption}\n{index}. [{title}]({link}) {free} {seeder}\n{description}"
                index += 1

            return self.__send_message(title, caption, userid)

        except Exception as msg_e:
            logger.error(f"发送消息失败：{msg_e}")
            return False

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
                    logger.error(f"发送消息失败，错误信息：{ret_json.get('errmsg')}")
                    return False
            elif res is not None:
                logger.error(f"发送消息失败，错误码：{res.status_code}，错误原因：{res.reason}")
                return False
            else:
                logger.error(f"发送消息失败，未获取到返回信息")
                return False
        except Exception as err:
            logger.error(f"发送消息失败，错误信息：{err}")
            return False
