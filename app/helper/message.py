from __future__ import annotations

import ast
import json
import queue
import re
import threading
import time
from datetime import datetime
from typing import Any, Literal, Optional, List, Dict, Union
from typing import Callable

from cachetools import TTLCache
from jinja2 import Template

from app.core.config import global_vars
from app.core.context import MediaInfo, TorrentInfo
from app.core.meta import MetaBase
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.schemas.message import Notification
from app.schemas.tmdb import TmdbEpisode
from app.schemas.transfer import TransferInfo
from app.schemas.types import SystemConfigKey
from app.utils.singleton import Singleton, SingletonClass
from app.utils.string import StringUtils


class TemplateContextBuilder:
    """
    模板上下文构建器
    """

    def __init__(self):
        self._context = {}

    def build(
            self,
            meta: Optional[MetaBase] = None,
            mediainfo: Optional[MediaInfo] = None,
            torrentinfo: Optional[TorrentInfo] = None,
            transferinfo: Optional[TransferInfo] = None,
            file_extension: Optional[str] = None,
            episodes_info: Optional[List[TmdbEpisode]] = None,
            include_raw_objects: bool = True,
            **kwargs
    ) -> Dict[str, Any]:
        """
        :param meta: 媒体信息
        :param mediainfo: 媒体信息
        :param torrentinfo: 种子信息
        :param transferinfo: 传输信息
        :param file_extension: 文件扩展名
        :param episodes_info: 剧集信息
        :param include_raw_objects: 是否包含原始对象
        :return: 渲染上下文字典
        """
        self._context.clear()
        self._add_episode_details(meta, episodes_info)
        self._add_media_info(mediainfo)
        self._add_transfer_info(transferinfo)
        self._add_torrent_info(torrentinfo)
        self._add_file_info(file_extension)
        if kwargs: self._context.update(kwargs)

        if include_raw_objects:
            self._add_raw_objects(meta, mediainfo, torrentinfo, transferinfo, episodes_info)

        # 移除空值
        return {k: v for k, v in self._context.items() if v is not None}

    def _add_media_info(self, mediainfo: MediaInfo):
        """
        增加媒体信息
        """
        if not mediainfo: return
        base_info = {
            # 标题
            "title": self.__convert_invalid_characters(mediainfo.title),
            # 英文标题
            "en_title": self.__convert_invalid_characters(mediainfo.en_title),
            # 原语种标题
            "original_title": self.__convert_invalid_characters(mediainfo.original_title),
            # 季号
            "season": self._context.get("season") or mediainfo.season,
            # 年份
            "year": mediainfo.year or self._context.get("year"),
            # 媒体标题 + 年份
            "title_year": mediainfo.title_year or self._context.get("title_year"),
        }

        _meta_season = self._context.get("season")
        media_info = {
            # 类型
            "type": mediainfo.type.value,
            # 类别
            "category": mediainfo.category,
            # 评分
            "vote_average": mediainfo.vote_average,
            # 海报
            "poster": mediainfo.get_poster_image(),
            # 背景图
            "backdrop": mediainfo.get_backdrop_image(),
            # 季年份根据season值获取
            "season_year": mediainfo.season_years.get(
                int(_meta_season),
                None) if (mediainfo.season_years and _meta_season) else None,
            # 演员
            "actors": '、 '.join([actor['name'] for actor in mediainfo.actors[:5]]),
            # 简介
            "overview": mediainfo.overview,
            # TMDBID
            "tmdbid": mediainfo.tmdb_id,
            # IMDBID
            "imdbid": mediainfo.imdb_id,
            # 豆瓣ID
            "doubanid": mediainfo.douban_id,
        }
        self._context.update({**base_info, **media_info})

    def _add_episode_details(self, meta: Optional[MetaBase], episodes: Optional[List[TmdbEpisode]]):
        """
        添加剧集详细信息
        """
        if not meta:
            return

        episode_data = {"episode_title": None, "episode_date": None}
        if meta.begin_episode and episodes:
            for episode in episodes:
                if episode.episode_number == meta.begin_episode:
                    episode_data.update({
                        "episode_title": self.__convert_invalid_characters(episode.name),
                        "episode_date": episode.air_date if episode.air_date else None
                    })
                    break

        meta_info = {
            # 原文件名
            "original_name": meta.title,
            # 识别名称（优先使用中文）
            "name": meta.name,
            # 识别的英文名称（可能为空）
            "en_name": meta.en_name,
            # 年份
            "year": meta.year,
            # 名字 + 年份
            "title_year": self._context.get("title_year") or "%s (%s)" % (
                meta.name, meta.year) if meta.year else meta.name,
            # 季号
            "season": meta.season_seq,
            # 集号
            "episode": meta.episode_seqs,
            # 季集 SxxExx
            "season_episode": "%s%s" % (meta.season, meta.episode),
            # 段/节
            "part": meta.part,
            # 自定义占位符
            "customization": meta.customization,
        }

        tech_metadata = {
            # 资源类型
            "resourceType": meta.resource_type,
            # 特效
            "effect": meta.resource_effect,
            # 版本
            "edition": meta.edition,
            # 分辨率
            "videoFormat": meta.resource_pix,
            # 质量
            "resource_term": meta.resource_term,
            # 制作组/字幕组
            "releaseGroup": meta.resource_team,
            # 视频编码
            "videoCodec": meta.video_encode,
            # 音频编码
            "audioCodec": meta.audio_encode,
        }
        self._context.update({**meta_info, **tech_metadata, **episode_data})

    def _add_torrent_info(self, torrentinfo: Optional[TorrentInfo]):
        """
        添加种子信息
        """
        if not torrentinfo:
            return
        if torrentinfo.size:
            if str(torrentinfo.size).replace(".", "").isdigit():
                size = StringUtils.str_filesize(torrentinfo.size)
            else:
                size = torrentinfo.size
        else:
            size = 0

        if torrentinfo.description:
            html_re = re.compile(r'<[^>]+>', re.S)
            description = html_re.sub('', torrentinfo.description)
            torrentinfo.description = re.sub(r'<[^>]+>', '', description)

        torrent_info = {
            # 种子标题
            "torrent_title": torrentinfo.title,
            # 发布时间
            "pubdate": torrentinfo.pubdate,
            # 免费剩余时间
            "freedate": torrentinfo.freedate_diff,
            # 做种数
            "seeders": torrentinfo.seeders,
            # 促销信息
            "volume_factor": torrentinfo.volume_factor,
            # Hit&Run
            "hit_and_run": "是" if torrentinfo.hit_and_run else "否",
            # 种子标签
            "labels": ' '.join(torrentinfo.labels),
            # 描述
            "description": torrentinfo.description,
            # 站点名称
            "site_name": torrentinfo.site_name,
            # 种子大小
            "size": size,
        }
        self._context.update(torrent_info)

    def _add_transfer_info(self, transferinfo: Optional[TransferInfo]) -> Optional[Dict]:
        """
        添加文件转移上下文
        """
        if not transferinfo:
            return None
        ctx = {
            "transfer_type": transferinfo.transfer_type,
            "file_count": transferinfo.file_count,
            "total_size": StringUtils.str_filesize(transferinfo.total_size),
            "err_msg": transferinfo.message,
        }
        self._context.update(ctx)

    def _add_file_info(self, file_extension: Optional[str]):
        """
        添加文件信息
        """
        if not file_extension: return
        file_info = {
            # 文件后缀
            "fileExt": file_extension,
        }
        self._context.update(file_info)

    def _add_raw_objects(
            self,
            meta: Optional[MetaBase],
            mediainfo: Optional[MediaInfo],
            torrentinfo: Optional[TorrentInfo],
            transferinfo: Optional[TransferInfo],
            episodes_info: Optional[List[TmdbEpisode]],
    ):
        """
        添加原始对象引用
        """
        raw_objects = {
            # 文件元数据
            "__meta__": meta,
            # 识别的媒体信息
            "__mediainfo__": mediainfo,
            # 种子信息
            "__torrentinfo__": torrentinfo,
            # 文件转移信息
            "__transferinfo__": transferinfo,
            # 当前季的全部集信息
            "__episodes_info__": episodes_info,
        }
        self._context.update(raw_objects)

    @staticmethod
    def __convert_invalid_characters(filename: str):
        """
        将不支持的字符转换为全角字符
        """
        if not filename:
            return filename
        invalid_characters = r'\/:*?"<>|'
        # 创建半角到全角字符的转换表
        halfwidth_chars = "".join([chr(i) for i in range(33, 127)])
        fullwidth_chars = "".join([chr(i + 0xFEE0) for i in range(33, 127)])
        translation_table = str.maketrans(halfwidth_chars, fullwidth_chars)
        # 将不支持的字符替换为对应的全角字符
        for char in invalid_characters:
            filename = filename.replace(char, char.translate(translation_table))
        return filename


class TemplateHelper(metaclass=SingletonClass):
    """
    模板格式渲染帮助类
    """

    def __init__(self):
        self.builder = TemplateContextBuilder()
        self.cache = TTLCache(maxsize=100, ttl=600)

    @staticmethod
    def _generate_cache_key(cuntent: Union[str, dict]) -> str:
        """
        生成缓存键
        """
        if isinstance(cuntent, dict):
            base_str = cuntent.get("title", '') + cuntent.get("text", '')
            return StringUtils.md5_hash(json.dumps(base_str, sort_keys=True, ensure_ascii=False))

        return StringUtils.md5_hash(cuntent)

    def get_cache_context(self, cuntent: Union[str, dict]) -> Optional[dict]:
        """
        获取缓存上下文
        """
        cache_key = self._generate_cache_key(cuntent)
        return self.cache.get(cache_key)

    def set_cache_context(self, cuntent: Union[str, dict], context: dict) -> None:
        """
        设置缓存上下文
        """
        cache_key = self._generate_cache_key(cuntent)
        self.cache[cache_key] = context

    def render(self,
               template_content: str,
               template_type: Literal['string', 'dict', 'literal'] = "literal",
               **kwargs) -> Optional[Union[str, dict]]:
        """
        根据模板格式渲染内容
        :param template_content: 模板字符串
        :param template_type: 模板字符串类型(消息通知`literal`, 路径`string`)
        :param kwargs: 补传业务对象
        :raises ValueError: 当模板处理过程中出现错误
        :return: 渲染后的结果
        """
        try:
            # 解析模板字符
            parsed = self.parse_template_content(template_content, template_type)
            if not parsed:
                raise ValueError("模板解析失败")

            context = self.builder.build(**kwargs)
            if not context:
                raise ValueError("上下文构建失败")

            rendered = self.render_with_context(parsed, context)
            if not rendered:
                raise ValueError("模板渲染失败")

            if rendered := rendered if template_type == 'string' else self.__process_formatted_string(rendered):
                # 缓存上下文
                self.set_cache_context(rendered, context)
                # 返回渲染结果
                return rendered

        except Exception as e:
            logger.error(f"模板处理失败: {str(e)}")
            raise ValueError(f"模板处理失败: {str(e)}") from e

    @staticmethod
    def render_with_context(template_content: str, context: dict) -> str:
        """
        使用指定上下文渲染 Jinja2 模板字符串
        template_content: Jinja2 模板字符串
        context: 渲染用的上下文数据
        """
        # 渲染模板
        template = Template(template_content)
        return template.render(context)

    @staticmethod
    def parse_template_content(template_content: Union[str, dict],
                               template_type: Literal['string', 'dict', 'literal'] = None) -> Optional[str]:
        """
        解析模板字符
        :param template_content 模板格式字符
        :param template_type 模板字符类型
        """

        def parse_literal(_template_content: str) -> str:
            """
            解析Python字面量
            """
            try:
                template_dict = ast.literal_eval(_template_content) if isinstance(_template_content,
                                                                                  str) else _template_content
                if not isinstance(template_dict, dict):
                    raise ValueError("解析结果必须是一个字典")
                return json.dumps(template_dict, ensure_ascii=False)
            except (ValueError, SyntaxError) as err:
                raise ValueError(f"无效的Python字面量格式: {str(err)}")

        try:
            if template_type:
                parse_map = {
                    'string': lambda x: str(x),
                    'dict': lambda x: json.dumps(x, ensure_ascii=False),
                    'literal': parse_literal
                }
                return parse_map[template_type](template_content)

            # 自动判断模板类型
            if isinstance(template_content, dict):
                return json.dumps(template_content, ensure_ascii=False)
            elif isinstance(template_content, str):
                try:
                    json.loads(template_content)
                    return template_content
                except json.JSONDecodeError:
                    try:
                        return parse_literal(template_content)
                    except (ValueError, SyntaxError):
                        return template_content
            else:
                raise ValueError(f"不支持的模板类型: {type(template_content)}")

        except Exception as e:
            logger.error(f"模板解析失败: {str(e)}")
            return None

    @staticmethod
    def __process_formatted_string(rendered: str) -> Optional[Union[dict, str]]:
        """
        处理格式化字符串
        保留转义字符
        """

        def restore_chars(obj: Any) -> Any:
            """恢复特殊字符"""
            if isinstance(obj, str):
                return obj.replace('\\n', '\n').replace('\\r', '\r').replace('\\t', '\t').replace('\\b', '\b').replace(
                    '\\f', '\f')
            elif isinstance(obj, dict):
                return {k: restore_chars(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [restore_chars(item) for item in obj]
            return obj

            # 定义特殊字符映射

        special_chars = {
            '\n': '\\n',  # 换行符
            '\r': '\\r',  # 回车符
            '\t': '\\t',  # 制表符
            '\b': '\\b',  # 退格符
            '\f': '\\f',  # 换页符
        }

        # 处理特殊字符
        processed = rendered
        for char, escape in special_chars.items():
            processed = processed.replace(char, escape)

        # 尝试解析为JSON
        try:
            rendered_dict = json.loads(processed)
            return restore_chars(rendered_dict)
        except json.JSONDecodeError:
            return rendered


class MessageTemplateHelper:
    """
    消息模板渲染器
    """

    @staticmethod
    def render(message: Notification, *args, **kwargs) -> Optional[Notification]:
        """
        渲染消息模板
        """
        if not MessageTemplateHelper.is_instance_valid(message):
            if MessageTemplateHelper.meets_update_conditions(message, *args, **kwargs):
                logger.info("将使用模板渲染消息内容")
                return MessageTemplateHelper._apply_template_data(message, *args, **kwargs)
        return message

    @staticmethod
    def is_instance_valid(message: Notification) -> bool:
        """
        检查消息是否有效
        """
        if isinstance(message, Notification):
            return bool(message.title or message.text)
        return False

    @staticmethod
    def meets_update_conditions(message: Notification, *args, **kwargs) -> bool:
        """
        判断是否满足消息实例更新条件

        满足条件需同时具备：
        1. 消息为有效Notification实例
        2. 消息指定了模板类型(ctype)
        3. 存在待渲染的模板变量数据
        """
        if isinstance(message, Notification):
            return True if message.ctype and (args or kwargs) else False
        return False

    @staticmethod
    def _apply_template_data(message: Notification, *args, **kwargs) -> Optional[Notification]:
        """
        更新消息实例
        """
        try:
            if template := MessageTemplateHelper._get_template(message):
                rendered = TemplateHelper().render(template_content=template, *args, **kwargs)
                for key, value in rendered.items():
                    if hasattr(message, key):
                        setattr(message, key, value)
            return message
        except Exception as e:
            logger.error(f"更新Notification时出现错误：{str(e)}")
            return message

    @staticmethod
    def _get_template(message: Notification) -> Optional[str]:
        """
        获取消息模板
        """
        template_dict: dict[str, str] = SystemConfigOper().get(SystemConfigKey.NotificationTemplates)
        return template_dict.get(f"{message.ctype.value}")


class MessageQueueManager(metaclass=SingletonClass):
    """
    消息发送队列管理器
    """

    schedule_periods: List[tuple[int, int, int, int]] = []

    def __init__(
            self,
            send_callback: Optional[Callable] = None,
            check_interval: Optional[int] = 10
    ) -> None:
        """
        消息队列管理器初始化

        :param send_callback: 实际发送消息的回调函数
        :param check_interval: 时间检查间隔（秒）
        """
        self.init_config()

        self.queue: queue.Queue[Any] = queue.Queue()
        self.send_callback = send_callback
        self.check_interval = check_interval

        self._running = True
        self.thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.thread.start()

    def init_config(self):
        """
        初始化配置
        """
        self.schedule_periods = self._parse_schedule(
            SystemConfigOper().get(SystemConfigKey.NotificationSendTime)
        )

    @staticmethod
    def _parse_schedule(periods: Union[list, dict]) -> List[tuple[int, int, int, int]]:
        """
        将字符串时间格式转换为分钟数元组
        """
        parsed = []
        if not periods:
            return parsed
        if not isinstance(periods, list):
            periods = [periods]
        for period in periods:
            if not period:
                continue
            if not period.get('start') or not period.get('end'):
                continue
            start_h, start_m = map(int, period['start'].split(':'))
            end_h, end_m = map(int, period['end'].split(':'))
            parsed.append((start_h, start_m, end_h, end_m))
        return parsed

    @staticmethod
    def _time_to_minutes(time_str: str) -> int:
        """
        将 'HH:MM' 格式转换为分钟数
        """
        hours, minutes = map(int, time_str.split(':'))
        return hours * 60 + minutes

    def _is_in_scheduled_time(self, current_time: datetime) -> bool:
        """
        检查当前时间是否在允许发送的时间段内
        """
        if not self.schedule_periods:
            return True
        current_minutes = current_time.hour * 60 + current_time.minute
        for period in self.schedule_periods:
            s_h, s_m, e_h, e_m = period
            start = s_h * 60 + s_m
            end = e_h * 60 + e_m

            if start <= end:
                if start <= current_minutes <= end:
                    return True
            else:
                if current_minutes >= start or current_minutes <= end:
                    return True
        return False

    def send_message(self, *args, **kwargs) -> None:
        """
        发送消息（立即发送或加入队列）
        """
        if self._is_in_scheduled_time(datetime.now()):
            self._send(*args, **kwargs)
        else:
            self.queue.put({
                "args": args,
                "kwargs": kwargs
            })
            logger.info(f"消息已加入队列，当前队列长度：{self.queue.qsize()}")

    def _send(self, *args, **kwargs) -> None:
        """
        实际发送消息（可通过回调函数自定义）
        """
        if self.send_callback:
            try:
                logger.info(f"发送消息：{kwargs}")
                self.send_callback(*args, **kwargs)
            except Exception as e:
                logger.error(f"发送消息错误：{str(e)}")

    def _monitor_loop(self) -> None:
        """
        后台线程循环检查时间并处理队列
        """
        while self._running:
            current_time = datetime.now()
            if self._is_in_scheduled_time(current_time):
                while not self.queue.empty():
                    if global_vars.is_system_stopped:
                        break
                    if not self._is_in_scheduled_time(datetime.now()):
                        break
                    try:
                        message = self.queue.get_nowait()
                        self._send(*message['args'], **message['kwargs'])
                        logger.info(f"队列剩余消息：{self.queue.qsize()}")
                    except queue.Empty:
                        break
            time.sleep(self.check_interval)

    def stop(self) -> None:
        """
        停止队列管理器
        """
        self._running = False
        self.thread.join()


class MessageHelper(metaclass=Singleton):
    """
    消息队列管理器，包括系统消息和用户消息
    """

    def __init__(self):
        self.sys_queue = queue.Queue()
        self.user_queue = queue.Queue()

    def put(self, message: Any, role: str = "plugin", title: str = None, note: Union[list, dict] = None):
        """
        存消息
        :param message: 消息
        :param role: 消息通道 systm：系统消息，plugin：插件消息，user：用户消息
        :param title: 标题
        :param note: 附件json
        """
        if role in ["system", "plugin"]:
            # 没有标题时获取插件名称
            if role == "plugin" and not title:
                title = "插件通知"
            # 系统通知，默认
            self.sys_queue.put(json.dumps({
                "type": role,
                "title": title,
                "text": message,
                "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "note": note
            }))
        else:
            if isinstance(message, str):
                # 非系统的文本通知
                self.user_queue.put(json.dumps({
                    "title": title,
                    "text": message,
                    "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                    "note": note
                }))
            elif hasattr(message, "to_dict"):
                # 非系统的复杂结构通知，如媒体信息/种子列表等。
                content = message.to_dict()
                content['title'] = title
                content['date'] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                content['note'] = note
                self.user_queue.put(json.dumps(content))

    def get(self, role: str = "system") -> Optional[str]:
        """
        取消息
        :param role: 消息通道 systm：系统消息，plugin：插件消息，user：用户消息
        """
        if role == "system":
            if not self.sys_queue.empty():
                return self.sys_queue.get(block=False)
        else:
            if not self.user_queue.empty():
                return self.user_queue.get(block=False)
        return None
