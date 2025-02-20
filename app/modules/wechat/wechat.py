import json
import re
import threading
from datetime import datetime
from typing import Optional, List, Dict

from app.core.context import MediaInfo, Context
from app.core.metainfo import MetaInfo
from app.log import logger
from app.utils.common import retry
from app.utils.http import RequestUtils
from app.utils.string import StringUtils
from app.utils.url import UrlUtils

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
    # 代理
    _proxy = None

    # 企业微信发送消息URL
    _send_msg_url = "cgi-bin/message/send?access_token={access_token}"
    # 企业微信获取TokenURL
    _token_url = "cgi-bin/gettoken?corpid={corpid}&corpsecret={corpsecret}"
    # 企业微信创建菜单URL
    _create_menu_url = "cgi-bin/menu/create?access_token={access_token}&agentid={agentid}"
    # 企业微信删除菜单URL
    _delete_menu_url = "cgi-bin/menu/delete?access_token={access_token}&agentid={agentid}"

    def __init__(self, WECHAT_CORPID: str = None, WECHAT_APP_SECRET: str = None,
                 WECHAT_APP_ID: str = None, WECHAT_PROXY: str = None, **kwargs):
        """
        初始化
        """
        if not WECHAT_CORPID or not WECHAT_APP_SECRET or not WECHAT_APP_ID:
            logger.error("企业微信配置不完整！")
            return
        self._corpid = WECHAT_CORPID
        self._appsecret = WECHAT_APP_SECRET
        self._appid = WECHAT_APP_ID
        self._proxy = WECHAT_PROXY or "https://qyapi.weixin.qq.com"

        if self._proxy:
            self._send_msg_url = UrlUtils.adapt_request_url(self._proxy, self._send_msg_url)
            self._token_url = UrlUtils.adapt_request_url(self._proxy, self._token_url)
            self._create_menu_url = UrlUtils.adapt_request_url(self._proxy, self._create_menu_url)
            self._delete_menu_url = UrlUtils.adapt_request_url(self._proxy, self._delete_menu_url)

        if self._corpid and self._appsecret and self._appid:
            self.__get_access_token()

    def get_state(self):
        """
        获取状态
        """
        return True if self.__get_access_token() else False

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
                token_url = self._token_url.format(corpid=self._corpid, corpsecret=self._appsecret)
                res = RequestUtils().get_res(token_url)
                if res:
                    ret_json = res.json()
                    if ret_json.get("errcode") == 0:
                        self._access_token = ret_json.get("access_token")
                        self._expires_in = ret_json.get("expires_in")
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

    @staticmethod
    def __split_content(content: str, max_bytes: int = 2048) -> List[str]:
        """
        将内容分块为不超过 max_bytes 字节的块
        :param content: 待拆分的内容
        :param max_bytes: 最大字节数
        :return: 分块后的内容列表
        """
        content_chunks = []
        current_chunk = bytearray()

        for line in content.splitlines():
            encoded_line = (line + "\n").encode("utf-8")
            line_length = len(encoded_line)

            if line_length > max_bytes:
                # 在处理长行之前，先将 current_chunk 添加到 content_chunks
                if current_chunk:
                    content_chunks.append(current_chunk.decode("utf-8", errors="replace").strip())
                    current_chunk = bytearray()

                # 处理长行，拆分为多个不超过 max_bytes 的块
                start = 0
                while start < line_length:
                    end = start + max_bytes  # 不再需要为 "..." 预留空间
                    if end >= line_length:
                        end = line_length
                    else:
                        # 调整以避免拆分多字节字符
                        while end > start and (encoded_line[end] & 0xC0) == 0x80:
                            end -= 1
                        if end == start:
                            # 单个字符超过了 max_bytes，强制包含整个字符
                            end = start + 1
                            while end < line_length and (encoded_line[end] & 0xC0) == 0x80:
                                end += 1
                    truncated_line = encoded_line[start:end].decode("utf-8", errors="replace")
                    content_chunks.append(truncated_line.strip())
                    start = end
                continue  # 继续处理下一行

            # 检查添加当前行后是否会超过 max_bytes
            if len(current_chunk) + line_length > max_bytes:
                # 将 current_chunk 添加到 content_chunks
                content_chunks.append(current_chunk.decode("utf-8", errors="replace").strip())
                current_chunk = bytearray()

            # 将当前行添加到 current_chunk
            current_chunk += encoded_line

        # 处理剩余的 current_chunk
        if current_chunk:
            content_chunks.append(current_chunk.decode("utf-8", errors="replace").strip())

        return content_chunks

    def __send_message(self, title: str, text: str = None,
                       userid: str = None, link: str = None) -> bool:
        """
        发送文本消息
        :param title: 消息标题
        :param text: 消息内容
        :param userid: 消息发送对象的ID，为空则发给所有人
        :param link: 跳转链接
        :return: 发送状态，错误信息
        """
        if not title:
            logger.error("消息标题不能为空")
            return False
        if text:
            formatted_text = text.replace("\n\n", "\n")
            content = f"{title}\n{formatted_text}"
        else:
            content = title
        if link:
            content = f"{content}\n点击查看：{link}"
        if not userid:
            userid = "@all"
        # 分块处理逻辑
        content_chunks = self.__split_content(content)
        # 逐块发送消息
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
            try:
                # 如果是超长消息，有一个发送失败就全部失败
                if not self.__post_request(self._send_msg_url, req_json):
                    return False
            except Exception as e:
                logger.error(f"发送消息块失败：{e}")
                return False
        return True

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
        try:
            return self.__post_request(self._send_msg_url, req_json)
        except Exception as e:
            logger.error(f"发送图文消息失败：{e}")
            return False

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
        try:
            if not self.__get_access_token():
                logger.error("获取微信access_token失败，请检查参数配置")
                return None

            if image:
                ret_code = self.__send_image_message(title=title, text=text, image_url=image, userid=userid, link=link)
            else:
                ret_code = self.__send_message(title=title, text=text, userid=userid, link=link)

            return ret_code
        except Exception as e:
            logger.error(f"发送消息失败：{e}")
            return False

    def send_medias_msg(self, medias: List[MediaInfo], userid: str = "") -> Optional[bool]:
        """
        发送列表类消息
        """
        try:
            if not self.__get_access_token():
                logger.error("获取微信access_token失败，请检查参数配置")
                return None

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
            return self.__post_request(self._send_msg_url, req_json)
        except Exception as e:
            logger.error(f"发送消息失败：{e}")
            return False

    def send_torrents_msg(self, torrents: List[Context],
                          userid: str = "", title: str = "", link: str = None) -> Optional[bool]:
        """
        发送列表消息
        """
        try:
            if not self.__get_access_token():
                logger.error("获取微信access_token失败，请检查参数配置")
                return None

            # 先发送标题
            if title:
                self.__send_message(title=title, userid=userid, link=link)

            # 发送列表
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
                torrent_title = re.sub(r"\s+", " ", torrent_title).strip()
                articles.append({
                    "title": torrent_title,
                    "description": torrent.description if index == 1 else "",
                    "picurl": mediainfo.get_message_image() if index == 1 else "",
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
            return self.__post_request(self._send_msg_url, req_json)
        except Exception as e:
            logger.error(f"发送消息失败：{e}")
            return False

    @retry(Exception, logger=logger)
    def __post_request(self, url: str, req_json: dict) -> bool:
        """
        向微信发送请求
        """
        url = url.format(access_token=self.__get_access_token())
        res = RequestUtils(content_type="application/json").post(
            url=url,
            data=json.dumps(req_json, ensure_ascii=False).encode("utf-8")
        )
        if res is None:
            error_msg = "发送请求失败，未获取到返回信息"
            raise Exception(error_msg)
        if res.status_code != 200:
            error_msg = f"发送请求失败，错误码：{res.status_code}，错误原因：{res.reason}"
            raise Exception(error_msg)

        ret_json = res.json()
        if ret_json.get("errcode") == 0:
            return True
        else:
            if ret_json.get("errcode") == 42001:
                self.__get_access_token(force=True)
                error_msg = (f"access_token 已过期，尝试重新获取 access_token,"
                             f"errcode: {ret_json.get('errcode')}, errmsg: {ret_json.get('errmsg')}")
                raise Exception(error_msg)
            else:
                logger.error(f"发送请求失败，错误信息：{ret_json.get('errmsg')}")
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
        try:
            # 请求URL
            req_url = self._create_menu_url.format(access_token="{access_token}", agentid=self._appid)

            # 对commands按category分组
            category_dict = {}
            for key, value in commands.items():
                category: str = value.get("category")
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
        except Exception as e:
            logger.error(f"创建菜单失败：{e}")
            return False

    def delete_menus(self):
        """
        删除微信菜单
        """
        try:
            # 请求URL
            req_url = self._delete_menu_url.format(access_token=self.__get_access_token(), agentid=self._appid)
            # 发送请求
            RequestUtils().get(req_url)
        except Exception as e:
            logger.error(f"删除菜单失败：{e}")
            return False
