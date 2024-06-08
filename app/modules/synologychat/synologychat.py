import json
import re
from typing import Optional, List
from urllib.parse import quote
from threading import Lock

from app.core.config import settings
from app.core.context import MediaInfo, Context
from app.core.metainfo import MetaInfo
from app.log import logger
from app.utils.http import RequestUtils
from app.utils.string import StringUtils

lock = Lock()


class SynologyChat:
    def __init__(self):
        self._req = RequestUtils(content_type="application/x-www-form-urlencoded")
        self._webhook_url = settings.SYNOLOGYCHAT_WEBHOOK
        self._token = settings.SYNOLOGYCHAT_TOKEN
        if self._webhook_url:
            self._domain = StringUtils.get_base_url(self._webhook_url)

    def check_token(self, token: str) -> bool:
        return True if token == self._token else False

    def get_state(self) -> bool:
        """
        获取状态
        """
        if not self._webhook_url or not self._token:
            return False
        ret = self.__get_bot_users()
        if ret:
            return True
        return False

    def send_msg(self, title: str, text: str = "", image: str = "",
                 userid: str = "", link: str = None) -> Optional[bool]:
        """
        发送Telegram消息
        :param title: 消息标题
        :param text: 消息内容
        :param image: 消息图片地址
        :param userid: 用户ID，如有则只发消息给该用户
        :user_id: 发送消息的目标用户ID，为空则发给管理员
        :param link: 链接地址
        """
        if not title and not text:
            logger.error("标题和内容不能同时为空")
            return False
        if not self._webhook_url or not self._token:
            return False
        try:
            # 拼装消息内容
            titles = str(title).split('\n')
            if len(titles) > 1:
                title = titles[0]
                if not text:
                    text = "\n".join(titles[1:])
                else:
                    text = f"%s\n%s" % ("\n".join(titles[1:]), text)

            if text:
                caption = "*%s*\n%s" % (title, text.replace("\n\n", "\n"))
            else:
                caption = title

            if link:
                caption = f"{caption}\n[查看详情]({link})"

            payload_data = {'text': quote(caption)}
            if image:
                payload_data['file_url'] = quote(image)
            if userid:
                payload_data['user_ids'] = [int(userid)]
            else:
                userids = self.__get_bot_users()
                if not userids:
                    logger.error("SynologyChat机器人没有对任何用户可见")
                    return False
                payload_data['user_ids'] = userids

            return self.__send_request(payload_data)

        except Exception as msg_e:
            logger.error(f"SynologyChat发送消息错误：{str(msg_e)}")
            return False

    def send_meidas_msg(self, medias: List[MediaInfo], userid: str = "", title: str = "") -> Optional[bool]:
        """
        发送列表类消息
        """
        if not medias:
            return False
        if not self._webhook_url or not self._token:
            return False
        try:
            if not title or not isinstance(medias, list):
                return False
            index, image, caption = 1, "", "*%s*" % title
            for media in medias:
                if not image:
                    image = media.get_message_image()
                if media.vote_average:
                    caption = "%s\n%s. <%s|%s>\n_%s，%s_" % (caption,
                                                            index,
                                                            media.detail_link,
                                                            media.title_year,
                                                            f"类型：{media.type.value}",
                                                            f"评分：{media.vote_average}")
                else:
                    caption = "%s\n%s. <%s|%s>\n_%s_" % (caption,
                                                         index,
                                                         media.detail_link,
                                                         media.title_year,
                                                         f"类型：{media.type.value}")
                index += 1

            if userid:
                userids = [int(userid)]
            else:
                userids = self.__get_bot_users()
            payload_data = {
                "text": quote(caption),
                "user_ids": userids
            }
            return self.__send_request(payload_data)

        except Exception as msg_e:
            logger.error(f"SynologyChat发送消息错误：{str(msg_e)}")
            return False

    def send_torrents_msg(self, torrents: List[Context],
                          userid: str = "", title: str = "", link: str = None) -> Optional[bool]:
        """
        发送列表消息
        """
        if not self._webhook_url or not self._token:
            return None

        if not torrents:
            return False

        try:
            index, caption = 1, "*%s*" % title
            for context in torrents:
                torrent = context.torrent_info
                site_name = torrent.site_name
                meta = MetaInfo(torrent.title, torrent.description)
                link = torrent.page_url
                title = f"{meta.season_episode} " \
                        f"{meta.resource_term} " \
                        f"{meta.video_term} " \
                        f"{meta.release_group}"
                title = re.sub(r"\s+", " ", title).strip()
                free = torrent.volume_factor
                seeder = f"{torrent.seeders}↑"
                description = torrent.description
                caption = f"{caption}\n{index}.【{site_name}】<{link}|{title}> " \
                          f"{StringUtils.str_filesize(torrent.size)} {free} {seeder}\n" \
                          f"_{description}_"
                index += 1

            if link:
                caption = f"{caption}\n[查看详情]({link})"

            if userid:
                userids = [int(userid)]
            else:
                userids = self.__get_bot_users()

            payload_data = {
                "text": quote(caption),
                "user_ids": userids
            }
            return self.__send_request(payload_data)
        except Exception as msg_e:
            logger.error(f"SynologyChat发送消息错误：{str(msg_e)}")
            return False

    def __get_bot_users(self):
        """
        查询机器人可见的用户列表
        """
        if not self._domain or not self._token:
            return []
        req_url = f"{self._domain}" \
                  f"/webapi/entry.cgi?api=SYNO.Chat.External&method=user_list&version=2&token=" \
                  f"{self._token}"
        ret = self._req.get_res(url=req_url)
        if ret and ret.status_code == 200:
            users = ret.json().get("data", {}).get("users", []) or []
            return [user.get("user_id") for user in users if user.get("deleted", True) is False]
        else:
            return []

    def __send_request(self, payload_data):
        """
        发送消息请求
        """
        payload = f"payload={json.dumps(payload_data)}"
        ret = self._req.post_res(url=self._webhook_url, data=payload)
        if ret and ret.status_code == 200:
            result = ret.json()
            if result:
                errno = result.get('error', {}).get('code')
                errmsg = result.get('error', {}).get('errors')
                if not errno:
                    return True
                logger.error(f"SynologyChat返回错误：{errno}-{errmsg}")
                return False
            else:
                logger.error(f"SynologyChat返回：{ret.text}")
                return False
        elif ret is not None:
            logger.error(f"SynologyChat请求失败，错误码：{ret.status_code}，错误原因：{ret.reason}")
            return False
        else:
            logger.error(f"SynologyChat请求失败，未获取到返回信息")
            return False
