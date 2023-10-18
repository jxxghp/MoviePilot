import json
import re
from datetime import datetime

from app.core.config import settings
from app.plugins import _PluginBase
from app.core.event import eventmanager
from app.schemas.types import EventType, MessageChannel
from app.utils.http import RequestUtils
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger


class MessageForward(_PluginBase):
    # 插件名称
    plugin_name = "消息转发"
    # 插件描述
    plugin_desc = "根据正则转发通知到其他WeChat应用。"
    # 插件图标
    plugin_icon = "forward.png"
    # 主题色
    plugin_color = "#32ABD1"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "messageforward_"
    # 加载顺序
    plugin_order = 16
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _wechat = None
    _pattern = None
    _pattern_token = {}

    # 企业微信发送消息URL
    _send_msg_url = f"{settings.WECHAT_PROXY}/cgi-bin/message/send?access_token=%s"
    # 企业微信获取TokenURL
    _token_url = f"{settings.WECHAT_PROXY}/cgi-bin/gettoken?corpid=%s&corpsecret=%s"

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._wechat = config.get("wechat")
            self._pattern = config.get("pattern")

            # 获取token存库
            if self._enabled and self._wechat:
                self.__save_wechat_token()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
                   {
                       'component': 'VForm',
                       'content': [
                           {
                               'component': 'VRow',
                               'content': [
                                   {
                                       'component': 'VCol',
                                       'props': {
                                           'cols': 12,
                                           'md': 6
                                       },
                                       'content': [
                                           {
                                               'component': 'VSwitch',
                                               'props': {
                                                   'model': 'enabled',
                                                   'label': '开启转发'
                                               }
                                           }
                                       ]
                                   },
                               ]
                           },
                           {
                               'component': 'VRow',
                               'content': [
                                   {
                                       'component': 'VCol',
                                       'props': {
                                           'cols': 12,
                                       },
                                       'content': [
                                           {
                                               'component': 'VTextarea',
                                               'props': {
                                                   'model': 'wechat',
                                                   'rows': '3',
                                                   'label': '应用配置',
                                                   'placeholder': 'appid:corpid:appsecret（一行一个配置）'
                                               }
                                           }
                                       ]
                                   }
                               ]
                           },
                           {
                               'component': 'VRow',
                               'content': [
                                   {
                                       'component': 'VCol',
                                       'props': {
                                           'cols': 12,
                                       },
                                       'content': [
                                           {
                                               'component': 'VTextarea',
                                               'props': {
                                                   'model': 'pattern',
                                                   'rows': '3',
                                                   'label': '正则配置',
                                                   'placeholder': '对应上方应用配置，一行一个，一一对应'
                                               }
                                           }
                                       ]
                                   }
                               ]
                           },
                       ]
                   }
               ], {
                   "enabled": False,
                   "wechat": "",
                   "pattern": ""
               }

    def get_page(self) -> List[dict]:
        pass

    @eventmanager.register(EventType.NoticeMessage)
    def send(self, event):
        """
        消息转发
        """
        if not self._enabled:
            return

        # 消息体
        data = event.event_data
        channel = data['channel']
        if channel and channel != MessageChannel.Wechat:
            return

        title = data['title']
        text = data['text']
        image = data['image']
        userid = data['userid']

        # 正则匹配
        patterns = self._pattern.split("\n")
        for index, pattern in enumerate(patterns):
            msg_match = re.search(pattern, title)
            if msg_match:
                access_token, appid = self.__flush_access_token(index)
                if not access_token:
                    logger.error("未获取到有效token，请检查配置")
                    continue

                # 发送消息
                if image:
                    self.__send_image_message(title, text, image, userid, access_token, appid, index)
                else:
                    self.__send_message(title, text, userid, access_token, appid, index)

    def __save_wechat_token(self):
        """
        获取并存储wechat token
        """
        # 解析配置
        wechats = self._wechat.split("\n")
        for index, wechat in enumerate(wechats):
            wechat_config = wechat.split(":")
            if len(wechat_config) != 3:
                logger.error(f"{wechat} 应用配置不正确")
                continue
            appid = wechat_config[0]
            corpid = wechat_config[1]
            appsecret = wechat_config[2]

            # 已过期，重新获取token
            access_token, expires_in, access_token_time = self.__get_access_token(corpid=corpid,
                                                                                  appsecret=appsecret)
            if not access_token:
                # 没有token，获取token
                logger.error(f"wechat配置 appid = {appid} 获取token失败，请检查配置")
                continue

            self._pattern_token[index] = {
                "appid": appid,
                "corpid": corpid,
                "appsecret": appsecret,
                "access_token": access_token,
                "expires_in": expires_in,
                "access_token_time": access_token_time,
            }

    def __flush_access_token(self, index: int, force: bool = False):
        """
        获取第i个配置wechat token
        """
        wechat_token = self._pattern_token[index]
        if not wechat_token:
            logger.error(f"未获取到第 {index} 条正则对应的wechat应用token，请检查配置")
            return None
        access_token = wechat_token['access_token']
        expires_in = wechat_token['expires_in']
        access_token_time = wechat_token['access_token_time']
        appid = wechat_token['appid']
        corpid = wechat_token['corpid']
        appsecret = wechat_token['appsecret']

        # 判断token有效期
        if force or (datetime.now() - access_token_time).seconds >= expires_in:
            # 重新获取token
            access_token, expires_in, access_token_time = self.__get_access_token(corpid=corpid,
                                                                                  appsecret=appsecret)
            if not access_token:
                logger.error(f"wechat配置 appid = {appid} 获取token失败，请检查配置")
                return None, None

        self._pattern_token[index] = {
            "appid": appid,
            "corpid": corpid,
            "appsecret": appsecret,
            "access_token": access_token,
            "expires_in": expires_in,
            "access_token_time": access_token_time,
        }
        return access_token, appid

    def __send_message(self, title: str, text: str = None, userid: str = None, access_token: str = None,
                       appid: str = None, index: int = None) -> Optional[bool]:
        """
        发送文本消息
        :param title: 消息标题
        :param text: 消息内容
        :param userid: 消息发送对象的ID，为空则发给所有人
        :return: 发送状态，错误信息
        """
        if text:
            conent = "%s\n%s" % (title, text.replace("\n\n", "\n"))
        else:
            conent = title

        if not userid:
            userid = "@all"
        req_json = {
            "touser": userid,
            "msgtype": "text",
            "agentid": appid,
            "text": {
                "content": conent
            },
            "safe": 0,
            "enable_id_trans": 0,
            "enable_duplicate_check": 0
        }
        return self.__post_request(access_token=access_token, req_json=req_json, index=index, title=title)

    def __send_image_message(self, title: str, text: str, image_url: str, userid: str = None,
                             access_token: str = None, appid: str = None, index: int = None) -> Optional[bool]:
        """
        发送图文消息
        :param title: 消息标题
        :param text: 消息内容
        :param image_url: 图片地址
        :param userid: 消息发送对象的ID，为空则发给所有人
        :return: 发送状态，错误信息
        """
        if text:
            text = text.replace("\n\n", "\n")
        if not userid:
            userid = "@all"
        req_json = {
            "touser": userid,
            "msgtype": "news",
            "agentid": appid,
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
        return self.__post_request(access_token=access_token, req_json=req_json, index=index, title=title)

    def __post_request(self, access_token: str, req_json: dict, index: int, title: str, retry: int = 0) -> bool:
        message_url = self._send_msg_url % access_token
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
                    logger.info(f"转发消息 {title} 成功")
                    return True
                else:
                    if ret_json.get('errcode') == 81013:
                        return False

                    logger.error(f"转发消息 {title} 失败，错误信息：{ret_json}")
                    if ret_json.get('errcode') == 42001 or ret_json.get('errcode') == 40014:
                        logger.info("token已过期，正在重新刷新token重试")
                        # 重新获取token
                        access_token, appid = self.__flush_access_token(index=index,
                                                                        force=True)
                        if access_token:
                            retry += 1
                            # 重发请求
                            if retry <= 3:
                                return self.__post_request(access_token=access_token,
                                                           req_json=req_json,
                                                           index=index,
                                                           title=title,
                                                           retry=retry)
                    return False
            elif res is not None:
                logger.error(f"转发消息 {title} 失败，错误码：{res.status_code}，错误原因：{res.reason}")
                return False
            else:
                logger.error(f"转发消息 {title} 失败，未获取到返回信息")
                return False
        except Exception as err:
            logger.error(f"转发消息 {title} 异常，错误信息：{str(err)}")
            return False

    def __get_access_token(self, corpid: str, appsecret: str):
        """
        获取微信Token
        :return： 微信Token
        """
        try:
            token_url = self._token_url % (corpid, appsecret)
            res = RequestUtils().get_res(token_url)
            if res:
                ret_json = res.json()
                if ret_json.get('errcode') == 0:
                    access_token = ret_json.get('access_token')
                    expires_in = ret_json.get('expires_in')
                    access_token_time = datetime.now()

                    return access_token, expires_in, access_token_time
                else:
                    logger.error(f"{ret_json.get('errmsg')}")
                    return None, None, None
            else:
                logger.error(f"{corpid} {appsecret} 获取token失败")
                return None, None, None
        except Exception as e:
            logger.error(f"获取微信access_token失败，错误信息：{str(e)}")
            return None, None, None

    def stop_service(self):
        """
        退出插件
        """
        pass
