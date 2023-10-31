import os
import shutil
import time
from pathlib import Path

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple

from app.schemas.types import EventType, MediaImageType, NotificationType, MediaType
from app.utils.system import SystemUtils


class CloudDiskDel(_PluginBase):
    # 插件名称
    plugin_name = "云盘文件删除"
    # 插件描述
    plugin_desc = "媒体库删除strm文件后同步删除云盘资源。"
    # 插件图标
    plugin_icon = "clouddisk.png"
    # 主题色
    plugin_color = "#ff9933"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "clouddiskdel_"
    # 加载顺序
    plugin_order = 26
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    # 任务执行间隔
    _paths = {}
    _notify = False

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._notify = config.get("notify")
            for path in str(config.get("path")).split("\n"):
                paths = path.split(":")
                self._paths[paths[0]] = paths[1]

    @eventmanager.register(EventType.NetworkDiskDel)
    def clouddisk_del(self, event: Event):
        if not self._enabled:
            return

        event_data = event.event_data
        logger.info(f"获取到云盘删除请求 {event_data}")

        media_path = event_data.get("media_path")
        if not media_path:
            logger.error("未获取到删除路径")
            return

        media_name = event_data.get("media_name")
        tmdb_id = event_data.get("tmdb_id")
        media_type = event_data.get("media_type")
        season_num = event_data.get("season_num")
        episode_num = event_data.get("episode_num")

        # 判断删除媒体路径是否与配置的媒体库路径相符，相符则继续删除，不符则跳过
        for library_path in list(self._paths.keys()):
            if str(media_path).startswith(library_path):
                # 替换网盘路径
                media_path = str(media_path).replace(library_path, self._paths.get(library_path))
                logger.info(f"获取到moviepilot本地云盘挂载路径 {media_path}")
                path = Path(media_path)
                if path.is_file() or media_path.endswith(".strm"):
                    # 删除文件、nfo、jpg等同名文件
                    pattern = path.stem.replace('[', '?').replace(']', '?')
                    logger.info(f"开始筛选同名文件 {pattern}")
                    files = path.parent.glob(f"{pattern}.*")
                    for file in files:
                        Path(file).unlink()
                        logger.info(f"云盘文件 {file} 已删除")
                else:
                    # 非根目录，才删除目录
                    shutil.rmtree(path)
                    # 删除目录
                    logger.warn(f"云盘目录 {path} 已删除")

                # 判断当前媒体父路径下是否有媒体文件，如有则无需遍历父级
                if not SystemUtils.exits_files(path.parent, settings.RMT_MEDIAEXT):
                    # 判断父目录是否为空, 为空则删除
                    for parent_path in path.parents:
                        if str(parent_path.parent) != str(path.root):
                            # 父目录非根目录，才删除父目录
                            if not SystemUtils.exits_files(parent_path, settings.RMT_MEDIAEXT):
                                # 当前路径下没有媒体文件则删除
                                shutil.rmtree(parent_path)
                                logger.warn(f"云盘目录 {parent_path} 已删除")

                break

        # 发送消息
        image = 'https://emby.media/notificationicon.png'
        media_type = MediaType.MOVIE if media_type in ["Movie", "MOV"] else MediaType.TV
        if self._notify:
            backrop_image = self.chain.obtain_specific_image(
                mediaid=tmdb_id,
                mtype=media_type,
                image_type=MediaImageType.Backdrop,
                season=season_num,
                episode=episode_num
            ) or image

            # 类型
            if media_type == MediaType.MOVIE:
                msg = f'电影 {media_name} {tmdb_id}'
            # 删除电视剧
            elif media_type == MediaType.TV and not season_num and not episode_num:
                msg = f'剧集 {media_name} {tmdb_id}'
            # 删除季 S02
            elif media_type == MediaType.TV and season_num and not episode_num:
                msg = f'剧集 {media_name} S{season_num} {tmdb_id}'
            # 删除剧集S02E02
            elif media_type == MediaType.TV and season_num and episode_num:
                msg = f'剧集 {media_name} S{season_num}E{episode_num} {tmdb_id}'
            else:
                msg = media_name

            # 发送通知
            self.post_message(
                mtype=NotificationType.MediaServer,
                title="云盘同步删除任务完成",
                image=backrop_image,
                text=f"{msg}\n"
                     f"时间 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))}"
            )

        # 读取历史记录
        history = self.get_data('history') or []

        # 获取poster
        poster_image = self.chain.obtain_specific_image(
            mediaid=tmdb_id,
            mtype=media_type,
            image_type=MediaImageType.Poster,
        ) or image
        history.append({
            "type": media_type.value,
            "title": media_name,
            "path": media_path,
            "season": season_num,
            "episode": episode_num,
            "image": poster_image,
            "del_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
        })

        # 保存历史
        self.save_data("history", history)

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
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
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
                                            'model': 'notify',
                                            'label': '开启通知',
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
                                            'model': 'path',
                                            'rows': '2',
                                            'label': '媒体库路径映射',
                                            'placeholder': '媒体服务器路径:moviepilot内云盘挂载路径（一行一个）'
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
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '需要开启媒体库删除插件且正确配置排除路径。'
                                                    '主要针对于strm文件删除后同步删除云盘资源。'
                                                    '如遇删除失败，请检查文件权限问题。'
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
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '关于路径映射：'
                                                    'emby:/data/series/A.mp4,'
                                                    'moviepilot内云盘挂载路径:/mnt/link/series/A.mp4。'
                                                    '路径映射填/data:/mnt/link'
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
            "path": "",
            "notify": False
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        # 查询同步详情
        historys = self.get_data('history')
        if not historys:
            return [
                {
                    'component': 'div',
                    'text': '暂无数据',
                    'props': {
                        'class': 'text-center',
                    }
                }
            ]
        # 数据按时间降序排序
        historys = sorted(historys, key=lambda x: x.get('del_time'), reverse=True)
        # 拼装页面
        contents = []
        for history in historys:
            htype = history.get("type")
            title = history.get("title")
            season = history.get("season")
            episode = history.get("episode")
            image = history.get("image")
            del_time = history.get("del_time")

            if season:
                sub_contents = [
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'类型：{htype}'
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'标题：{title}'
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'季：{season}'
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'集：{episode}'
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'时间：{del_time}'
                    }
                ]
            else:
                sub_contents = [
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'类型：{htype}'
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'标题：{title}'
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'时间：{del_time}'
                    }
                ]

            contents.append(
                {
                    'component': 'VCard',
                    'content': [
                        {
                            'component': 'div',
                            'props': {
                                'class': 'd-flex justify-space-start flex-nowrap flex-row',
                            },
                            'content': [
                                {
                                    'component': 'div',
                                    'content': [
                                        {
                                            'component': 'VImg',
                                            'props': {
                                                'src': image,
                                                'height': 120,
                                                'width': 80,
                                                'aspect-ratio': '2/3',
                                                'class': 'object-cover shadow ring-gray-500',
                                                'cover': True
                                            }
                                        }
                                    ]
                                },
                                {
                                    'component': 'div',
                                    'content': sub_contents
                                }
                            ]
                        }
                    ]
                }
            )

        return [
            {
                'component': 'div',
                'props': {
                    'class': 'grid gap-3 grid-info-card',
                },
                'content': contents
            }
        ]

    def stop_service(self):
        """
        退出插件
        """
        pass
