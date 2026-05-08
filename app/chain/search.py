import asyncio
import hashlib
import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import AsyncIterator, Any, Dict, Tuple
from typing import List, Optional

from app.helper.sites import SitesHelper  # noqa
from fastapi.concurrency import run_in_threadpool

from app.chain import ChainBase
from app.core.config import global_vars, settings
from app.core.context import Context
from app.core.context import MediaInfo, TorrentInfo
from app.core.event import eventmanager, Event
from app.core.metainfo import MetaInfo
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.progress import ProgressHelper
from app.helper.torrent import TorrentHelper
from app.log import logger
from app.schemas import NotExistMediaInfo
from app.schemas.types import MediaType, ProgressKey, SystemConfigKey, EventType
from app.utils.string import StringUtils


class SearchChain(ChainBase):
    """
    站点资源搜索处理链
    """

    __result_temp_file = "__search_result__"
    __ai_indices_cache_file = "__ai_recommend_indices__"

    _ai_recommend_running = False
    _ai_recommend_task: Optional[asyncio.Task] = None
    _current_recommend_request_hash: Optional[str] = None
    _ai_recommend_result: Optional[List[int]] = None
    _ai_recommend_error: Optional[str] = None

    @property
    def is_ai_recommend_enabled(self) -> bool:
        """
        检查AI推荐功能是否已启用。
        """
        return settings.AI_AGENT_ENABLE and settings.AI_RECOMMEND_ENABLED

    @staticmethod
    def _calculate_recommend_request_hash(
        filtered_indices: Optional[List[int]], search_results_count: int
    ) -> str:
        """
        计算当前推荐请求哈希，用于识别筛选条件是否变化。
        """
        request_data = {
            "filtered_indices": filtered_indices or [],
            "search_results_count": search_results_count,
        }
        return hashlib.md5(
            json.dumps(request_data, sort_keys=True).encode()
        ).hexdigest()

    def _build_ai_recommend_status(self) -> Dict[str, Any]:
        """
        构建AI推荐状态字典。
        """
        state = type(self)
        if not self.is_ai_recommend_enabled:
            return {"status": "disabled"}

        if state._ai_recommend_running:
            return {"status": "running"}

        if state._ai_recommend_result is None:
            cached_indices = self.load_cache(self.__ai_indices_cache_file)
            if cached_indices is not None:
                state._ai_recommend_result = cached_indices

        if state._ai_recommend_result is not None:
            return {"status": "completed", "results": state._ai_recommend_result}

        if state._ai_recommend_error is not None:
            return {"status": "error", "error": state._ai_recommend_error}

        return {"status": "idle"}

    def get_current_recommend_status_only(self) -> Dict[str, Any]:
        """
        获取当前推荐状态，不校验请求是否变化。
        """
        return self._build_ai_recommend_status()

    def get_recommend_status(
        self, filtered_indices: Optional[List[int]], search_results_count: int
    ) -> Dict[str, Any]:
        """
        获取AI推荐状态，并在筛选条件变化时返回 idle。
        """
        state = type(self)
        request_hash = self._calculate_recommend_request_hash(
            filtered_indices, search_results_count
        )
        if request_hash != state._current_recommend_request_hash:
            return {"status": "idle"} if self.is_ai_recommend_enabled else {"status": "disabled"}
        return self._build_ai_recommend_status()

    def cancel_ai_recommend(self):
        """
        取消当前AI推荐任务并清空缓存状态。
        """
        state = type(self)
        if state._ai_recommend_task and not state._ai_recommend_task.done():
            state._ai_recommend_task.cancel()
        state._ai_recommend_running = False
        state._ai_recommend_task = None
        state._current_recommend_request_hash = None
        state._ai_recommend_result = None
        state._ai_recommend_error = None
        self.remove_cache(self.__ai_indices_cache_file)

    @staticmethod
    def _normalize_ai_indices(ai_indices: List[Any]) -> List[int]:
        """
        过滤模型返回的非法或重复索引，保留原顺序。
        """
        normalized = []
        seen = set()
        for index in ai_indices:
            try:
                value = int(index)
            except (TypeError, ValueError):
                continue
            if value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return normalized

    @staticmethod
    def _extract_recommend_items(
        filtered_indices: Optional[List[int]], results: List[Any]
    ) -> tuple[List[str], List[int]]:
        """
        构建发送给模型的候选列表和索引映射。
        """
        items: List[str] = []
        valid_indices: List[int] = []
        max_items = settings.AI_RECOMMEND_MAX_ITEMS or 50

        if filtered_indices:
            results_to_process = [
                results[index] for index in filtered_indices if 0 <= index < len(results)
            ]
        else:
            results_to_process = results

        for index, torrent in enumerate(results_to_process):
            if len(items) >= max_items:
                break
            if not torrent.torrent_info:
                continue

            valid_indices.append(index)
            item_info = {
                "index": index,
                "title": torrent.torrent_info.title or "未知",
                "size": (
                    StringUtils.format_size(torrent.torrent_info.size)
                    if torrent.torrent_info.size
                    else "0 B"
                ),
                "seeders": torrent.torrent_info.seeders or 0,
            }
            items.append(json.dumps(item_info, ensure_ascii=False))

        return items, valid_indices

    @staticmethod
    def _restore_original_indices(
        ai_indices: List[int],
        filtered_indices: Optional[List[int]],
        valid_indices: List[int],
        results_count: int,
    ) -> List[int]:
        """
        将模型输出的局部索引映射回原始搜索结果索引。
        """
        original_indices = []
        seen = set()

        for index in ai_indices:
            if not 0 <= index < len(valid_indices):
                continue
            original_index = (
                filtered_indices[valid_indices[index]]
                if filtered_indices
                else valid_indices[index]
            )
            if not 0 <= original_index < results_count or original_index in seen:
                continue
            seen.add(original_index)
            original_indices.append(original_index)

        return original_indices

    async def _invoke_recommend_llm(self, search_results_text: str) -> str:
        """
        通过统一后台提示词机制执行资源推荐。
        """
        from app.agent import ReplyMode, agent_manager
        from app.agent.prompt import prompt_manager

        prompt = prompt_manager.render_system_task_message(
            "search_recommend",
            template_context={"search_results": search_results_text},
        )
        full_output = [""]

        def on_output(text: str):
            full_output[0] = text

        await agent_manager.run_background_prompt(
            message=prompt,
            session_prefix="__agent_search_recommend",
            output_callback=on_output,
            reply_mode=ReplyMode.CAPTURE_ONLY,
            persist_output_message=False,
            allow_message_tools=False,
        )
        return full_output[0].strip()

    def start_recommend_task(
        self,
        filtered_indices: Optional[List[int]],
        search_results_count: int,
        results: List[Any],
    ) -> None:
        """
        启动AI推荐任务。
        """
        if not self.is_ai_recommend_enabled:
            logger.warning("AI推荐功能未启用，跳过任务执行")
            return

        state = type(self)
        request_hash = self._calculate_recommend_request_hash(
            filtered_indices, search_results_count
        )
        if request_hash == state._current_recommend_request_hash:
            return

        self.cancel_ai_recommend()
        state._current_recommend_request_hash = request_hash

        async def run_recommend():
            current_task = asyncio.current_task()

            def is_current_request() -> bool:
                return state._current_recommend_request_hash == request_hash

            try:
                state._ai_recommend_running = True

                items, valid_indices = self._extract_recommend_items(
                    filtered_indices=filtered_indices,
                    results=results,
                )
                if not items:
                    if is_current_request():
                        state._ai_recommend_error = "没有可用于AI推荐的资源"
                    return

                user_preference = (
                    settings.AI_RECOMMEND_USER_PREFERENCE
                    or "Prefer high-quality resources with more seeders"
                )
                search_results_text = (
                    f"User Preference: {user_preference}\n\n"
                    f"Candidate Resources:\n{chr(10).join(items)}"
                )
                ai_response = await self._invoke_recommend_llm(search_results_text)
                if not ai_response:
                    if is_current_request():
                        state._ai_recommend_error = "AI推荐未返回结果"
                    return

                json_match = re.search(r"\[.*?]", ai_response, re.DOTALL)
                if not json_match:
                    raise ValueError(f"无法从响应中提取JSON数组: {ai_response}")

                ai_indices = json.loads(json_match.group())
                if not isinstance(ai_indices, list):
                    raise ValueError(f"AI返回格式错误: {ai_response}")

                original_indices = self._restore_original_indices(
                    ai_indices=self._normalize_ai_indices(ai_indices),
                    filtered_indices=filtered_indices,
                    valid_indices=valid_indices,
                    results_count=len(results),
                )
                if not is_current_request():
                    logger.info("AI推荐结果已过期，丢弃旧结果")
                    return

                state._ai_recommend_result = original_indices
                self.save_cache(original_indices, self.__ai_indices_cache_file)
                logger.info(f"AI推荐完成: {len(original_indices)}项")
            except asyncio.CancelledError:
                logger.info("AI推荐任务被取消")
            except Exception as err:
                logger.error(f"AI推荐任务失败: {err}")
                if is_current_request():
                    state._ai_recommend_error = str(err)
            finally:
                if state._ai_recommend_task == current_task:
                    state._ai_recommend_running = False
                    state._ai_recommend_task = None

        state._ai_recommend_task = asyncio.create_task(run_recommend())

    def search_by_id(self, tmdbid: Optional[int] = None, doubanid: Optional[str] = None,
                     mtype: MediaType = None, area: Optional[str] = "title", season: Optional[int] = None,
                     sites: List[int] = None, cache_local: bool = False) -> List[Context]:
        """
        根据TMDBID/豆瓣ID搜索资源，精确匹配，不过滤本地存在的资源
        :param tmdbid: TMDB ID
        :param doubanid: 豆瓣 ID
        :param mtype: 媒体，电影 or 电视剧
        :param area: 搜索范围，title or imdbid
        :param season: 季数
        :param sites: 站点ID列表
        :param cache_local: 是否缓存到本地
        """
        if cache_local:
            self.cancel_ai_recommend()
        mediainfo = self.recognize_media(tmdbid=tmdbid, doubanid=doubanid, mtype=mtype)
        if not mediainfo:
            logger.error(f'{tmdbid} 媒体信息识别失败！')
            return []
        no_exists = None
        if season is not None:
            no_exists = {
                tmdbid or doubanid: {
                    season: NotExistMediaInfo(episodes=[])
                }
            }
        results = self.process(mediainfo=mediainfo, sites=sites, area=area, no_exists=no_exists)
        # 保存到本地文件
        if cache_local:
            self.save_cache(results, self.__result_temp_file)
        return results

    def search_by_title(self, title: str, page: Optional[int] = 0,
                        sites: List[int] = None, cache_local: Optional[bool] = False) -> List[Context]:
        """
        根据标题搜索资源，不识别不过滤，直接返回站点内容
        :param title: 标题，为空时返回所有站点首页内容
        :param page: 页码
        :param sites: 站点ID列表
        :param cache_local: 是否缓存到本地
        """
        if cache_local:
            self.cancel_ai_recommend()
        if title:
            logger.info(f'开始搜索资源，关键词：{title} ...')
        else:
            logger.info(f'开始浏览资源，站点：{sites} ...')
        # 搜索
        torrents = self.__search_all_sites(keyword=title, sites=sites, page=page) or []
        if not torrents:
            logger.warn(f'{title} 未搜索到资源')
            return []
        # 组装上下文
        contexts = [
            Context(
                meta_info=MetaInfo(title=torrent.title, subtitle=torrent.description),
                torrent_info=torrent,
                resource_source="search",
            ) for torrent in torrents
        ]
        # 保存到本地文件
        if cache_local:
            self.save_cache(contexts, self.__result_temp_file)
        return contexts

    def last_search_results(self) -> Optional[List[Context]]:
        """
        获取上次搜索结果
        """
        return self.load_cache(self.__result_temp_file)

    async def async_last_search_results(self) -> Optional[List[Context]]:
        """
        异步获取上次搜索结果
        """
        return await self.async_load_cache(self.__result_temp_file)

    async def async_search_by_id(self, tmdbid: Optional[int] = None, doubanid: Optional[str] = None,
                                 mtype: MediaType = None, area: Optional[str] = "title", season: Optional[int] = None,
                                 sites: List[int] = None, cache_local: bool = False) -> List[Context]:
        """
        根据TMDBID/豆瓣ID异步搜索资源，精确匹配，不过滤本地存在的资源
        :param tmdbid: TMDB ID
        :param doubanid: 豆瓣 ID
        :param mtype: 媒体，电影 or 电视剧
        :param area: 搜索范围，title or imdbid
        :param season: 季数
        :param sites: 站点ID列表
        :param cache_local: 是否缓存到本地
        """
        if cache_local:
            self.cancel_ai_recommend()
        mediainfo = await self.async_recognize_media(tmdbid=tmdbid, doubanid=doubanid, mtype=mtype)
        if not mediainfo:
            logger.error(f'{tmdbid} 媒体信息识别失败！')
            return []
        no_exists = None
        if season is not None:
            no_exists = {
                tmdbid or doubanid: {
                    season: NotExistMediaInfo(episodes=[])
                }
            }
        results = await self.async_process(mediainfo=mediainfo, sites=sites, area=area, no_exists=no_exists)
        # 保存到本地文件
        if cache_local:
            await self.async_save_cache(results, self.__result_temp_file)
        return results

    async def async_search_by_title(self, title: str, page: Optional[int] = 0,
                                    sites: List[int] = None, cache_local: Optional[bool] = False) -> List[Context]:
        """
        根据标题异步搜索资源，不识别不过滤，直接返回站点内容
        :param title: 标题，为空时返回所有站点首页内容
        :param page: 页码
        :param sites: 站点ID列表
        :param cache_local: 是否缓存到本地
        """
        if cache_local:
            self.cancel_ai_recommend()
        if title:
            logger.info(f'开始搜索资源，关键词：{title} ...')
        else:
            logger.info(f'开始浏览资源，站点：{sites} ...')
        # 搜索
        torrents = await self.__async_search_all_sites(keyword=title, sites=sites, page=page) or []
        if not torrents:
            logger.warn(f'{title} 未搜索到资源')
            return []
        # 组装上下文
        contexts = [
            Context(
                meta_info=MetaInfo(title=torrent.title, subtitle=torrent.description),
                torrent_info=torrent,
                resource_source="search",
            ) for torrent in torrents
        ]
        # 保存到本地文件
        if cache_local:
            await self.async_save_cache(contexts, self.__result_temp_file)
        return contexts

    async def async_search_by_title_stream(self, title: str, page: Optional[int] = 0,
                                           sites: List[int] = None,
                                           cache_local: Optional[bool] = False) -> AsyncIterator[dict]:
        """
        根据标题渐进式搜索资源，不识别不过滤，按站点完成顺序返回结果
        """
        if cache_local:
            self.cancel_ai_recommend()
        if title:
            logger.info(f'开始渐进式搜索资源，关键词：{title} ...')
        else:
            logger.info(f'开始渐进式浏览资源，站点：{sites} ...')

        contexts: List[Context] = []
        async for event in self.__async_search_all_sites_stream(keyword=title, sites=sites, page=page):
            result = event.pop("items", []) or []
            batch_contexts = [
                Context(
                    meta_info=MetaInfo(title=torrent.title, subtitle=torrent.description),
                    torrent_info=torrent,
                    resource_source="search",
                )
                for torrent in result
            ]
            if batch_contexts:
                contexts.extend(batch_contexts)
            yield {
                **event,
                "type": "append",
                "items": [context.to_dict() for context in batch_contexts],
                "total_items": len(contexts)
            }

        if cache_local:
            await self.async_save_cache(contexts, self.__result_temp_file)

        if not contexts:
            logger.warn(f'{title} 未搜索到资源')
        yield {
            "type": "done",
            "text": f"搜索完成，共 {len(contexts)} 个资源",
            "items": [context.to_dict() for context in contexts],
            "total_items": len(contexts)
        }

    async def async_search_by_id_stream(self, tmdbid: Optional[int] = None, doubanid: Optional[str] = None,
                                        mtype: MediaType = None, area: Optional[str] = "title",
                                        season: Optional[int] = None, sites: List[int] = None,
                                        cache_local: bool = False) -> AsyncIterator[dict]:
        """
        根据TMDBID/豆瓣ID渐进式搜索资源，先返回站点原始候选，再返回过滤匹配后的最终结果
        """
        if cache_local:
            self.cancel_ai_recommend()
        mediainfo = await self.async_recognize_media(tmdbid=tmdbid, doubanid=doubanid, mtype=mtype)
        if not mediainfo:
            logger.error(f'{tmdbid} 媒体信息识别失败！')
            yield {
                "type": "error",
                "success": False,
                "message": "媒体信息识别失败"
            }
            return

        no_exists = None
        if season is not None:
            no_exists = {
                tmdbid or doubanid: {
                    season: NotExistMediaInfo(episodes=[])
                }
            }

        contexts: List[Context] = []
        async for event in self.async_process_stream(mediainfo=mediainfo, sites=sites, area=area, no_exists=no_exists):
            if event.get("type") == "done":
                contexts = event.get("contexts") or []
                event = {
                    key: value
                    for key, value in event.items()
                    if key != "contexts"
                }
            yield event

        if cache_local:
            await self.async_save_cache(contexts, self.__result_temp_file)

    @staticmethod
    def __prepare_params(mediainfo: MediaInfo,
                         keyword: Optional[str] = None,
                         no_exists: Dict[int, Dict[int, NotExistMediaInfo]] = None
                         ) -> Tuple[Dict[int, List[int]], List[str]]:
        """
        准备搜索参数
        """
        # 缺失的季集
        mediakey = mediainfo.tmdb_id or mediainfo.douban_id
        if no_exists and no_exists.get(mediakey):
            # 过滤剧集
            season_episodes = {sea: info.episodes
                               for sea, info in no_exists[mediakey].items()}
        elif mediainfo.season is not None:
            # 豆瓣只搜索当前季
            season_episodes = {mediainfo.season: []}
        else:
            season_episodes = None

        # 搜索关键词
        if keyword:
            keywords = [keyword]
        else:
            # 去重去空，但要保持顺序
            keywords = list(dict.fromkeys([k for k in [mediainfo.title,
                                                       mediainfo.original_title,
                                                       mediainfo.en_title,
                                                       mediainfo.hk_title,
                                                       mediainfo.tw_title,
                                                       mediainfo.sg_title] if k]))
            # 限制搜索关键词数量
            if settings.MAX_SEARCH_NAME_LIMIT:
                keywords = keywords[:settings.MAX_SEARCH_NAME_LIMIT]

        return season_episodes, keywords

    def __parse_result(self, torrents: List[TorrentInfo],
                       mediainfo: MediaInfo,
                       keyword: Optional[str] = None,
                       rule_groups: List[str] = None,
                       season_episodes: Dict[int, List[int]] = None,
                       custom_words: List[str] = None,
                       filter_params: Dict[str, str] = None) -> List[Context]:
        """
        处理搜索结果
        """

        def __do_filter(torrent_list: List[TorrentInfo]) -> List[TorrentInfo]:
            """
            执行优先级过滤
            """
            return self.filter_torrents(rule_groups=rule_groups,
                                        torrent_list=torrent_list,
                                        mediainfo=mediainfo) or []

        def __do_site_filter(torrent_list: List[TorrentInfo]) -> List[TorrentInfo]:
            """
            执行单个站点的过滤流程
            """
            if not torrent_list:
                return []

            filtered_torrents = torrent_list
            if filter_params:
                torrenthelper = TorrentHelper()
                filtered_torrents = [
                    torrent for torrent in filtered_torrents
                    if torrenthelper.filter_torrent(torrent, filter_params)
                ]

            if rule_groups and filtered_torrents:
                filtered_torrents = __do_filter(filtered_torrents)

            return filtered_torrents

        def __do_parallel_filter(torrent_list: List[TorrentInfo]) -> List[TorrentInfo]:
            """
            按站点并发执行过滤，保持站点内顺序不变
            """
            if not torrent_list or (not filter_params and not rule_groups):
                return torrent_list

            site_torrents: Dict[Tuple[Optional[int], Optional[str]], List[TorrentInfo]] = {}
            for torrent in torrent_list:
                site_key = (torrent.site, torrent.site_name)
                if site_key not in site_torrents:
                    site_torrents[site_key] = []
                site_torrents[site_key].append(torrent)

            if len(site_torrents) <= 1:
                return __do_site_filter(torrent_list)

            finished_count = 0
            filtered_by_site: Dict[Tuple[Optional[int], Optional[str]], List[TorrentInfo]] = {}
            max_workers = min(len(site_torrents), settings.CONF.threadpool or len(site_torrents))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                all_tasks = {
                    executor.submit(__do_site_filter, site_torrent_list): site_key
                    for site_key, site_torrent_list in site_torrents.items()
                }
                for future in as_completed(all_tasks):
                    finished_count += 1
                    filtered_by_site[all_tasks[future]] = future.result() or []
                    progress.update(
                        value=finished_count / len(site_torrents) * 50,
                        text=f'正在过滤，已完成 {finished_count} / {len(site_torrents)} 个站点 ...'
                    )

            filtered_ids = {
                id(torrent)
                for filtered_torrents in filtered_by_site.values()
                for torrent in filtered_torrents
            }
            return [torrent for torrent in torrent_list if id(torrent) in filtered_ids]

        if not torrents:
            logger.warn(f'{keyword or mediainfo.title} 未搜索到资源')
            return []

        # 开始新进度
        progress = ProgressHelper(ProgressKey.Search)
        progress.start()

        # 开始过滤
        progress.update(value=0, text=f'开始过滤，总 {len(torrents)} 个资源，请稍候...')
        # 匹配订阅附加参数
        if filter_params:
            logger.info(f'开始附加参数过滤，附加参数：{filter_params} ...')
        # 开始过滤规则过滤
        if rule_groups is None:
            # 取搜索过滤规则
            rule_groups: List[str] = SystemConfigOper().get(SystemConfigKey.SearchFilterRuleGroups)
        if rule_groups:
            logger.info(f'开始过滤规则/剧集过滤，使用规则组：{rule_groups} ...')
        torrents = __do_parallel_filter(torrents)
        if rule_groups:
            if not torrents:
                logger.warn(f'{keyword or mediainfo.title} 没有符合过滤规则的资源')
                return []
            logger.info(f"过滤规则/剧集过滤完成，剩余 {len(torrents)} 个资源")

        # 过滤完成
        progress.update(value=50, text=f'过滤完成，剩余 {len(torrents)} 个资源')

        # 总数
        _total = len(torrents)
        # 已处理数
        _count = 0

        # 开始匹配
        _match_torrents = []
        torrenthelper = TorrentHelper()
        try:
            # 英文标题应该在别名/原标题中，不需要再匹配
            logger.info(f"开始匹配结果 标题：{mediainfo.title}，原标题：{mediainfo.original_title}，别名：{mediainfo.names}")
            progress.update(value=51, text=f'开始匹配，总 {_total} 个资源 ...')
            for torrent in torrents:
                if global_vars.is_system_stopped:
                    break
                _count += 1
                progress.update(value=(_count / _total) * 96,
                                text=f'正在匹配 {torrent.site_name}，已完成 {_count} / {_total} ...')
                if not torrent.title:
                    continue

                # 识别元数据
                torrent_meta = MetaInfo(title=torrent.title, subtitle=torrent.description,
                                        custom_words=custom_words)
                if torrent.title != torrent_meta.org_string:
                    logger.info(f"种子名称应用识别词后发生改变：{torrent.title} => {torrent_meta.org_string}")
                # 季集数过滤
                if season_episodes \
                        and not TorrentHelper.match_season_episodes(torrent=torrent,
                                                                    meta=torrent_meta,
                                                                    season_episodes=season_episodes):
                    continue
                # 比对IMDBID
                if torrent.imdbid \
                        and mediainfo.imdb_id \
                        and torrent.imdbid == mediainfo.imdb_id:
                    logger.info(f'{mediainfo.title} 通过IMDBID匹配到资源：{torrent.site_name} - {torrent.title}')
                    _match_torrents.append((torrent, torrent_meta, "imdbid"))
                    continue

                # 比对种子
                if torrenthelper.match_torrent(mediainfo=mediainfo,
                                               torrent_meta=torrent_meta,
                                               torrent=torrent):
                    # 匹配成功
                    _match_torrents.append((torrent, torrent_meta, "title"))
                    continue
            # 匹配完成
            logger.info(f"匹配完成，共匹配到 {len(_match_torrents)} 个资源")
            progress.update(value=97,
                            text=f'匹配完成，共匹配到 {len(_match_torrents)} 个资源')

            # 去掉mediainfo中多余的数据
            mediainfo.clear()
            # 组装上下文
            contexts = [
                Context(
                    torrent_info=t[0],
                    media_info=mediainfo,
                    meta_info=t[1],
                    resource_source="search",
                    match_source=t[2],
                    candidate_recognized=False,
                    media_info_is_target=True,
                ) for t in _match_torrents
            ]
        finally:
            torrents.clear()
            del torrents
            _match_torrents.clear()
            del _match_torrents

        # 排序
        progress.update(value=99,
                        text=f'正在对 {len(contexts)} 个资源进行排序，请稍候...')
        contexts = torrenthelper.sort_torrents(contexts)

        # 结束进度
        logger.info(f'搜索完成，共 {len(contexts)} 个资源')
        progress.update(value=100,
                        text=f'搜索完成，共 {len(contexts)} 个资源')
        progress.end()

        # 去重后返回
        return self.__remove_duplicate(contexts)

    @staticmethod
    def __remove_duplicate(_torrents: List[Context]) -> List[Context]:
        """
        去除重复的种子
        :param _torrents: 种子列表
        :return: 去重后的种子列表
        """
        return list({f"{t.torrent_info.site_name}_{t.torrent_info.title}_{t.torrent_info.description}": t
                     for t in _torrents}.values())

    def process(self, mediainfo: MediaInfo,
                keyword: Optional[str] = None,
                no_exists: Dict[int, Dict[int, NotExistMediaInfo]] = None,
                sites: List[int] = None,
                rule_groups: List[str] = None,
                area: Optional[str] = "title",
                custom_words: List[str] = None,
                filter_params: Dict[str, str] = None) -> List[Context]:
        """
        根据媒体信息搜索种子资源，精确匹配，应用过滤规则，同时根据no_exists过滤本地已存在的资源
        :param mediainfo: 媒体信息
        :param keyword: 搜索关键词
        :param no_exists: 缺失的媒体信息
        :param sites: 站点ID列表，为空时搜索所有站点
        :param rule_groups: 过滤规则组名称列表
        :param area: 搜索范围，title or imdbid
        :param custom_words: 自定义识别词列表
        :param filter_params: 过滤参数
        """

        # 豆瓣标题处理
        if not mediainfo.tmdb_id:
            meta = MetaInfo(title=mediainfo.title)
            mediainfo.title = meta.name
            mediainfo.season = meta.begin_season
        logger.info(f'开始搜索资源，关键词：{keyword or mediainfo.title} ...')

        # 补充媒体信息
        if not mediainfo.names:
            mediainfo: MediaInfo = self.recognize_media(mtype=mediainfo.type,
                                                        tmdbid=mediainfo.tmdb_id,
                                                        doubanid=mediainfo.douban_id)
            if not mediainfo:
                logger.error(f'媒体信息识别失败！')
                return []

        # 准备搜索参数
        season_episodes, keywords = self.__prepare_params(
            mediainfo=mediainfo,
            keyword=keyword,
            no_exists=no_exists
        )

        # 站点搜索结果
        torrents: List[TorrentInfo] = []
        # 站点搜索次数
        search_count = 0

        # 多关键字执行搜索
        for search_word in keywords:
            # 强制休眠 1-10 秒
            if search_count > 0:
                logger.info(f"已搜索 {search_count} 次，强制休眠 1-10 秒 ...")
                time.sleep(random.randint(1, 10))

            # 搜索站点
            results = self.__search_all_sites(
                mediainfo=mediainfo,
                keyword=search_word,
                sites=sites,
                area=area
            ) or []
            # 合并结果

            search_count += 1
            torrents.extend(results)

            # 有结果则停止
            if not settings.SEARCH_MULTIPLE_NAME and torrents:
                logger.info(f"共搜索到 {len(torrents)} 个资源，停止搜索")
                break

        # 处理结果
        return self.__parse_result(
            torrents=torrents,
            mediainfo=mediainfo,
            keyword=keyword,
            rule_groups=rule_groups,
            season_episodes=season_episodes,
            custom_words=custom_words,
            filter_params=filter_params
        )

    async def async_process(self, mediainfo: MediaInfo,
                            keyword: Optional[str] = None,
                            no_exists: Dict[int, Dict[int, NotExistMediaInfo]] = None,
                            sites: List[int] = None,
                            rule_groups: List[str] = None,
                            area: Optional[str] = "title",
                            custom_words: List[str] = None,
                            filter_params: Dict[str, str] = None) -> List[Context]:
        """
        根据媒体信息异步搜索种子资源，精确匹配，应用过滤规则，同时根据no_exists过滤本地已存在的资源
        :param mediainfo: 媒体信息
        :param keyword: 搜索关键词
        :param no_exists: 缺失的媒体信息
        :param sites: 站点ID列表，为空时搜索所有站点
        :param rule_groups: 过滤规则组名称列表
        :param area: 搜索范围，title or imdbid
        :param custom_words: 自定义识别词列表
        :param filter_params: 过滤参数
        """

        # 豆瓣标题处理
        if not mediainfo.tmdb_id:
            meta = MetaInfo(title=mediainfo.title)
            mediainfo.title = meta.name
            mediainfo.season = meta.begin_season
        logger.info(f'开始搜索资源，关键词：{keyword or mediainfo.title} ...')

        # 补充媒体信息
        if not mediainfo.names:
            mediainfo: MediaInfo = await self.async_recognize_media(mtype=mediainfo.type,
                                                                    tmdbid=mediainfo.tmdb_id,
                                                                    doubanid=mediainfo.douban_id)
            if not mediainfo:
                logger.error(f'媒体信息识别失败！')
                return []

        # 准备搜索参数
        season_episodes, keywords = self.__prepare_params(
            mediainfo=mediainfo,
            keyword=keyword,
            no_exists=no_exists
        )

        # 站点搜索结果
        torrents: List[TorrentInfo] = []
        # 站点搜索次数
        search_count = 0

        # 多关键字执行搜索
        for search_word in keywords:
            # 强制休眠 1-10 秒
            if search_count > 0:
                logger.info(f"已搜索 {search_count} 次，强制休眠 1-10 秒 ...")
                await asyncio.sleep(random.randint(1, 10))
            # 搜索站点
            torrents.extend(
                await self.__async_search_all_sites(
                    mediainfo=mediainfo,
                    keyword=search_word,
                    sites=sites,
                    area=area
                ) or []
            )
            search_count += 1
            # 未开启多名称搜索时，有结果则停止
            if not settings.SEARCH_MULTIPLE_NAME and torrents:
                logger.info(f"共搜索到 {len(torrents)} 个资源，停止搜索")
                break

        # 处理结果
        return await run_in_threadpool(self.__parse_result,
                                       torrents=torrents,
                                       mediainfo=mediainfo,
                                       keyword=keyword,
                                       rule_groups=rule_groups,
                                       season_episodes=season_episodes,
                                       custom_words=custom_words,
                                       filter_params=filter_params
                                       )

    async def async_process_stream(self, mediainfo: MediaInfo,
                                   keyword: Optional[str] = None,
                                   no_exists: Dict[int, Dict[int, NotExistMediaInfo]] = None,
                                   sites: List[int] = None,
                                   rule_groups: List[str] = None,
                                   area: Optional[str] = "title",
                                   custom_words: List[str] = None,
                                   filter_params: Dict[str, str] = None) -> AsyncIterator[dict]:
        """
        根据媒体信息渐进式搜索种子资源，先返回站点候选，再返回过滤匹配后的最终结果
        """

        # 豆瓣标题处理
        if not mediainfo.tmdb_id:
            meta = MetaInfo(title=mediainfo.title)
            mediainfo.title = meta.name
            mediainfo.season = meta.begin_season
        logger.info(f'开始渐进式搜索资源，关键词：{keyword or mediainfo.title} ...')

        # 补充媒体信息
        if not mediainfo.names:
            mediainfo = await self.async_recognize_media(mtype=mediainfo.type,
                                                         tmdbid=mediainfo.tmdb_id,
                                                         doubanid=mediainfo.douban_id)
            if not mediainfo:
                logger.error(f'媒体信息识别失败！')
                yield {
                    "type": "error",
                    "success": False,
                    "message": "媒体信息识别失败"
                }
                return

        # 准备搜索参数
        season_episodes, keywords = self.__prepare_params(
            mediainfo=mediainfo,
            keyword=keyword,
            no_exists=no_exists
        )

        torrents: List[TorrentInfo] = []
        candidate_contexts: List[Context] = []
        search_count = 0

        for search_word in keywords:
            if search_count > 0:
                logger.info(f"已搜索 {search_count} 次，强制休眠 1-10 秒 ...")
                await asyncio.sleep(random.randint(1, 10))

            async for event in self.__async_search_all_sites_stream(
                    mediainfo=mediainfo,
                    keyword=search_word,
                    sites=sites,
                    area=area):
                result = event.pop("items", []) or []
                torrents.extend(result)
                batch_contexts = [
                    Context(
                        meta_info=MetaInfo(title=torrent.title, subtitle=torrent.description),
                        media_info=mediainfo,
                        torrent_info=torrent,
                        resource_source="search",
                        media_info_is_target=True,
                    )
                    for torrent in result
                ]
                candidate_contexts.extend(batch_contexts)
                yield {
                    **event,
                    "type": "append",
                    "stage": "searching",
                    "items": [context.to_dict() for context in batch_contexts],
                    "total_items": len(candidate_contexts)
                }

            search_count += 1
            if not settings.SEARCH_MULTIPLE_NAME and torrents:
                logger.info(f"共搜索到 {len(torrents)} 个资源，停止搜索")
                break

        yield {
            "type": "progress",
            "stage": "filtering",
            "value": 98,
            "text": f"正在过滤匹配 {len(torrents)} 个候选资源 ..."
        }

        contexts = await run_in_threadpool(self.__parse_result,
                                           torrents=torrents,
                                           mediainfo=mediainfo,
                                           keyword=keyword,
                                           rule_groups=rule_groups,
                                           season_episodes=season_episodes,
                                           custom_words=custom_words,
                                           filter_params=filter_params)
        final_items = [context.to_dict() for context in contexts]
        yield {
            "type": "replace",
            "stage": "filtered",
            "value": 100,
            "text": f"过滤匹配完成，共 {len(contexts)} 个资源",
            "items": final_items,
            "total_items": len(contexts)
        }
        yield {
            "type": "done",
            "stage": "done",
            "text": f"搜索完成，共 {len(contexts)} 个资源",
            "items": final_items,
            "total_items": len(contexts),
            "contexts": contexts
        }

    def __search_all_sites(self, keyword: str,
                           mediainfo: Optional[MediaInfo] = None,
                           sites: List[int] = None,
                           page: Optional[int] = 0,
                           area: Optional[str] = "title") -> Optional[List[TorrentInfo]]:
        """
        多线程搜索多个站点
        :param mediainfo:  识别的媒体信息
        :param keyword:  搜索关键词
        :param sites:  指定站点ID列表，如有则只搜索指定站点，否则搜索所有站点
        :param page:  搜索页码
        :param area:  搜索区域 title or imdbid
        :reutrn: 资源列表
        """
        # 未开启的站点不搜索
        indexer_sites = []

        # 配置的索引站点
        if not sites:
            sites = SystemConfigOper().get(SystemConfigKey.IndexerSites) or []

        for indexer in SitesHelper().get_indexers():
            # 检查站点索引开关
            if not sites or indexer.get("id") in sites:
                indexer_sites.append(indexer)
        if not indexer_sites:
            logger.warn('未开启任何有效站点，无法搜索资源')
            return []

        # 开始进度
        progress = ProgressHelper(ProgressKey.Search)
        progress.start()
        # 开始计时
        start_time = datetime.now()
        # 总数
        total_num = len(indexer_sites)
        # 完成数
        finish_count = 0
        # 更新进度
        progress.update(value=0,
                        text=f"开始搜索，共 {total_num} 个站点 ...")
        # 结果集
        results = []
        # 多线程
        with ThreadPoolExecutor(max_workers=len(indexer_sites)) as executor:
            all_task = []
            for site in indexer_sites:
                if area == "imdbid":
                    # 搜索IMDBID
                    task = executor.submit(self.search_torrents, site=site,
                                           keyword=mediainfo.imdb_id if mediainfo else None,
                                           mtype=mediainfo.type if mediainfo else None,
                                           page=page)
                else:
                    # 搜索标题
                    task = executor.submit(self.search_torrents, site=site,
                                           keyword=keyword,
                                           mtype=mediainfo.type if mediainfo else None,
                                           page=page)
                all_task.append(task)
            for future in as_completed(all_task):
                if global_vars.is_system_stopped:
                    break
                finish_count += 1
                result = future.result()
                if result:
                    results.extend(result)
                logger.info(f"站点搜索进度：{finish_count} / {total_num}")
                progress.update(value=finish_count / total_num * 100,
                                text=f"正在搜索{keyword or ''}，已完成 {finish_count} / {total_num} 个站点 ...")
        # 计算耗时
        end_time = datetime.now()
        # 更新进度
        progress.update(value=100,
                        text=f"站点搜索完成，有效资源数：{len(results)}，总耗时 {(end_time - start_time).seconds} 秒")
        logger.info(f"站点搜索完成，有效资源数：{len(results)}，总耗时 {(end_time - start_time).seconds} 秒")
        # 结束进度
        progress.end()

        # 返回
        return results

    async def __async_search_all_sites(self, keyword: str,
                                       mediainfo: Optional[MediaInfo] = None,
                                       sites: List[int] = None,
                                       page: Optional[int] = 0,
                                       area: Optional[str] = "title") -> Optional[List[TorrentInfo]]:
        """
        异步搜索多个站点
        :param mediainfo:  识别的媒体信息
        :param keyword:  搜索关键词
        :param sites:  指定站点ID列表，如有则只搜索指定站点，否则搜索所有站点
        :param page:  搜索页码
        :param area:  搜索区域 title or imdbid
        :reutrn: 资源列表
        """
        # 未开启的站点不搜索
        indexer_sites = []

        # 配置的索引站点
        if not sites:
            sites = SystemConfigOper().get(SystemConfigKey.IndexerSites) or []

        for indexer in await SitesHelper().async_get_indexers():
            # 检查站点索引开关
            if not sites or indexer.get("id") in sites:
                indexer_sites.append(indexer)
        if not indexer_sites:
            logger.warn('未开启任何有效站点，无法搜索资源')
            return []

        # 开始进度
        progress = ProgressHelper(ProgressKey.Search)
        progress.start()
        # 开始计时
        start_time = datetime.now()
        # 总数
        total_num = len(indexer_sites)
        # 完成数
        finish_count = 0
        # 更新进度
        progress.update(value=0,
                        text=f"开始搜索，共 {total_num} 个站点 ...")
        # 结果集
        results = []

        # 创建异步任务列表
        tasks = []
        for site in indexer_sites:
            if area == "imdbid":
                # 搜索IMDBID
                task = self.async_search_torrents(site=site,
                                                  keyword=mediainfo.imdb_id if mediainfo else None,
                                                  mtype=mediainfo.type if mediainfo else None,
                                                  page=page)
            else:
                # 搜索标题
                task = self.async_search_torrents(site=site,
                                                  keyword=keyword,
                                                  mtype=mediainfo.type if mediainfo else None,
                                                  page=page)
            tasks.append(task)

        # 使用asyncio.as_completed来处理并发任务
        for future in asyncio.as_completed(tasks):
            if global_vars.is_system_stopped:
                break
            finish_count += 1
            result = await future
            if result:
                results.extend(result)
            logger.info(f"站点搜索进度：{finish_count} / {total_num}")
            progress.update(value=finish_count / total_num * 100,
                            text=f"正在搜索{keyword or ''}，已完成 {finish_count} / {total_num} 个站点 ...")

        # 计算耗时
        end_time = datetime.now()
        # 更新进度
        progress.update(value=100,
                        text=f"站点搜索完成，有效资源数：{len(results)}，总耗时 {(end_time - start_time).seconds} 秒")
        logger.info(f"站点搜索完成，有效资源数：{len(results)}，总耗时 {(end_time - start_time).seconds} 秒")
        # 结束进度
        progress.end()

        # 返回
        return results

    async def __async_search_all_sites_stream(self, keyword: str,
                                              mediainfo: Optional[MediaInfo] = None,
                                              sites: List[int] = None,
                                              page: Optional[int] = 0,
                                              area: Optional[str] = "title") -> AsyncIterator[Dict[str, Any]]:
        """
        异步搜索多个站点，按站点完成顺序渐进式返回结果
        :param mediainfo:  识别的媒体信息
        :param keyword:  搜索关键词
        :param sites:  指定站点ID列表，如有则只搜索指定站点，否则搜索所有站点
        :param page:  搜索页码
        :param area:  搜索区域 title or imdbid
        """
        indexer_sites = []

        if not sites:
            sites = SystemConfigOper().get(SystemConfigKey.IndexerSites) or []

        for indexer in await SitesHelper().async_get_indexers():
            if not sites or indexer.get("id") in sites:
                indexer_sites.append(indexer)
        if not indexer_sites:
            logger.warn('未开启任何有效站点，无法搜索资源')
            yield {
                "type": "done",
                "stage": "searching",
                "value": 100,
                "text": "未开启任何有效站点，无法搜索资源",
                "items": [],
                "finished": 0,
                "total": 0
            }
            return

        progress = ProgressHelper(ProgressKey.Search)
        progress.start()
        start_time = datetime.now()
        total_num = len(indexer_sites)
        finish_count = 0
        progress.update(value=0,
                        text=f"开始搜索，共 {total_num} 个站点 ...")
        yield {
            "type": "progress",
            "stage": "searching",
            "value": 0,
            "text": f"开始搜索，共 {total_num} 个站点 ...",
            "items": [],
            "finished": 0,
            "total": total_num
        }

        async def search_site(site: dict) -> Tuple[dict, List[TorrentInfo]]:
            if area == "imdbid":
                result = await self.async_search_torrents(site=site,
                                                          keyword=mediainfo.imdb_id if mediainfo else None,
                                                          mtype=mediainfo.type if mediainfo else None,
                                                          page=page)
            else:
                result = await self.async_search_torrents(site=site,
                                                          keyword=keyword,
                                                          mtype=mediainfo.type if mediainfo else None,
                                                          page=page)
            return site, result or []

        tasks = [asyncio.create_task(search_site(site)) for site in indexer_sites]
        results_count = 0
        try:
            for future in asyncio.as_completed(tasks):
                if global_vars.is_system_stopped:
                    break
                finish_count += 1
                site, result = await future
                results_count += len(result)
                logger.info(f"站点搜索进度：{finish_count} / {total_num}")
                progress_value = finish_count / total_num * 100
                progress_text = f"正在搜索{keyword or ''}，已完成 {finish_count} / {total_num} 个站点 ..."
                progress.update(value=progress_value, text=progress_text)
                yield {
                    "type": "append",
                    "stage": "searching",
                    "value": progress_value,
                    "text": progress_text,
                    "items": result,
                    "site": site.get("name"),
                    "site_id": site.get("id"),
                    "finished": finish_count,
                    "total": total_num,
                    "total_items": results_count
                }
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

        end_time = datetime.now()
        progress.update(value=100,
                        text=f"站点搜索完成，有效资源数：{results_count}，总耗时 {(end_time - start_time).seconds} 秒")
        logger.info(f"站点搜索完成，有效资源数：{results_count}，总耗时 {(end_time - start_time).seconds} 秒")
        progress.end()

    @eventmanager.register(EventType.SiteDeleted)
    def remove_site(self, event: Event):
        """
        从搜索站点中移除与已删除站点相关的设置
        """
        if not event:
            return
        event_data = event.event_data or {}
        site_id = event_data.get("site_id")
        if not site_id:
            return
        if site_id == "*":
            # 清空搜索站点
            SystemConfigOper().set(SystemConfigKey.IndexerSites, [])
            return
        # 从选中的rss站点中移除
        selected_sites = SystemConfigOper().get(SystemConfigKey.IndexerSites) or []
        if site_id in selected_sites:
            selected_sites.remove(site_id)
            SystemConfigOper().set(SystemConfigKey.IndexerSites, selected_sites)
