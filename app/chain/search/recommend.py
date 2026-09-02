"""AI 搜索推荐协调 owner。"""

import asyncio
import hashlib
import json
import re
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.application.configuration import (
    get_chain_runtime_config_snapshot,
)
from app.chain.search.contract import _SearchOwnerBase
from app.foundation import size as size_tools
from app.runtime.log import logger
from app.runtime.tasks import get_task_registry


@dataclass(frozen=True)
class RecommendSnapshot:
    """AI 推荐协调器的只读状态快照。"""

    generation: int
    request_hash: Optional[str]
    running: bool
    task: Optional[asyncio.Task[Any]]
    result: Optional[List[int]]
    error: Optional[str]


class RecommendCoordinator:
    """串行化 AI 推荐代际、任务和结果发布，阻止旧请求覆盖新状态。"""

    def __init__(self) -> None:
        """初始化空闲推荐状态和跨线程保护锁。"""
        self._lock = threading.RLock()
        self._generation = 0
        self._request_hash: Optional[str] = None
        self._running = False
        self._task: Optional[asyncio.Task[Any]] = None
        self._result: Optional[List[int]] = None
        self._error: Optional[str] = None

    def snapshot(self) -> RecommendSnapshot:
        """返回同一锁快照中的完整推荐状态。"""
        with self._lock:
            return RecommendSnapshot(
                generation=self._generation,
                request_hash=self._request_hash,
                running=self._running,
                task=self._task,
                result=list(self._result) if self._result is not None else None,
                error=self._error,
            )

    def begin(self, request_hash: str) -> tuple[int, Optional[asyncio.Task[Any]], bool]:
        """开始新代际；相同请求仍处于运行或已有终态时保持幂等。"""
        with self._lock:
            if request_hash == self._request_hash and (
                self._running or self._result is not None or self._error is not None
            ):
                return self._generation, None, False
            previous_task = self._task
            self._generation += 1
            self._request_hash = request_hash
            self._running = True
            self._task = None
            self._result = None
            self._error = None
            return self._generation, previous_task, True

    def attach(self, generation: int, task: asyncio.Task[Any]) -> bool:
        """仅把任务绑定到仍为当前的代际。"""
        with self._lock:
            if generation != self._generation:
                return False
            self._task = task
            return True

    def is_current(self, generation: int, request_hash: str) -> bool:
        """判断任务是否仍拥有当前代际。"""
        with self._lock:
            return generation == self._generation and request_hash == self._request_hash

    def publish_result(self, generation: int, result: List[int]) -> bool:
        """仅允许当前代际发布推荐结果。"""
        with self._lock:
            if generation != self._generation:
                return False
            self._result = list(result)
            self._error = None
            return True

    def publish_error(self, generation: int, error: str) -> bool:
        """仅允许当前代际发布错误。"""
        with self._lock:
            if generation != self._generation:
                return False
            self._error = error
            self._result = None
            return True

    def restore(self, request_hash: str, result: List[int]) -> None:
        """在空闲状态恢复与当前请求精确匹配的持久缓存。"""
        with self._lock:
            self._generation += 1
            self._request_hash = request_hash
            self._running = False
            self._task = None
            self._result = list(result)
            self._error = None

    def finish(self, generation: int, task: Optional[asyncio.Task[Any]]) -> None:
        """当前代际任务结束后清理运行标记，不影响已经发布的终态。"""
        with self._lock:
            if generation != self._generation:
                return
            if self._task is task:
                self._task = None
            self._running = False

    def cancel(self) -> Optional[asyncio.Task[Any]]:
        """推进代际并清空状态，返回需要由调用方取消的旧任务。"""
        with self._lock:
            previous_task = self._task
            self._generation += 1
            self._request_hash = None
            self._running = False
            self._task = None
            self._result = None
            self._error = None
            return previous_task


recommend_coordinator = RecommendCoordinator()


class SearchRecommendOwner(_SearchOwnerBase):
    """AI 搜索推荐协调 owner。"""

    @property
    def is_ai_recommend_enabled(self) -> bool:  # type: ignore[override]
        """
        检查AI推荐功能是否已启用。
        """
        return self.runtime_config.ai_agent_enable and self.runtime_config.ai_recommend_enabled

    @staticmethod
    def _calculate_recommend_request_hash(
        filtered_indices: Optional[List[int]],
        search_results_count: int,
        results: Optional[List[Any]] = None,
    ) -> str:
        """
        计算包含候选资源身份的请求哈希，避免相同数量的不同搜索复用旧结果。
        """
        request_data = {
            "filtered_indices": filtered_indices or [],
            "search_results_count": search_results_count,
            "results": [
                SearchRecommendOwner._recommend_result_identity(result)
                for result in (results or [])[:search_results_count]
            ],
        }
        return hashlib.sha256(json.dumps(request_data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _recommend_result_identity(result: Any) -> dict[str, Any]:
        """投影推荐候选的稳定资源身份，不依赖对象地址或展示顺序之外的状态。"""
        torrent = getattr(result, "torrent_info", None)
        if torrent is None:
            return {"value": str(result)}
        return {
            "site": str(getattr(torrent, "site", "") or ""),
            "torrent_id": str(getattr(torrent, "torrent_id", "") or ""),
            "title": str(getattr(torrent, "title", "") or ""),
            "description": str(getattr(torrent, "description", "") or ""),
            "size": getattr(torrent, "size", None),
            "seeders": getattr(torrent, "seeders", None),
            "category": str(getattr(torrent, "category", "") or ""),
        }

    def _build_ai_recommend_status(self) -> Dict[str, Any]:
        """
        构建AI推荐状态字典。
        """
        if not self.is_ai_recommend_enabled:
            return {"status": "disabled"}

        state = recommend_coordinator.snapshot()
        if state.running:
            return {"status": "running"}
        if state.result is not None:
            return {"status": "completed", "results": state.result}
        if state.error is not None:
            return {"status": "error", "error": state.error}

        return {"status": "idle"}

    def get_current_recommend_status_only(self) -> Dict[str, Any]:
        """
        获取当前推荐状态，不校验请求是否变化。
        """
        return self._build_ai_recommend_status()

    def get_recommend_status(self, filtered_indices: Optional[List[int]], search_results_count: int) -> Dict[str, Any]:
        """
        获取AI推荐状态，并在筛选条件变化时返回 idle。
        """
        results = self.last_search_results() or []
        request_hash = self._calculate_recommend_request_hash(
            filtered_indices,
            search_results_count,
            results,
        )
        state = recommend_coordinator.snapshot()
        if request_hash != state.request_hash:
            cached = self.load_cache(self._AI_INDICES_CACHE_KEY)
            if (
                isinstance(cached, dict)
                and cached.get("request_hash") == request_hash
                and isinstance(cached.get("results"), list)
            ):
                recommend_coordinator.restore(request_hash, cached["results"])
                return self._build_ai_recommend_status()
            return {"status": "idle"} if self.is_ai_recommend_enabled else {"status": "disabled"}
        return self._build_ai_recommend_status()

    def cancel_ai_recommend(self) -> None:
        """
        取消当前AI推荐任务并清空缓存状态。
        """
        task = recommend_coordinator.cancel()
        if task and not task.done():
            task.cancel()
        self.remove_cache(self._AI_INDICES_CACHE_KEY)

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
        max_items = get_chain_runtime_config_snapshot().ai_recommend_max_items or 50

        if filtered_indices:
            results_to_process = [results[index] for index in filtered_indices if 0 <= index < len(results)]
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
                "size": (size_tools.format_size(torrent.torrent_info.size) if torrent.torrent_info.size else "0 B"),
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
            original_index = filtered_indices[valid_indices[index]] if filtered_indices else valid_indices[index]
            if not 0 <= original_index < results_count or original_index in seen:
                continue
            seen.add(original_index)
            original_indices.append(original_index)

        return original_indices

    @staticmethod
    async def _invoke_recommend_llm(search_results_text: str) -> str:
        """
        通过统一后台提示词机制执行资源推荐。
        """
        from app.application.agent import get_prompt_manager, get_running_agent_manager
        from app.schemas.types import ReplyMode

        prompt = get_prompt_manager().render_system_task_message(
            "search_recommend",
            template_context={"search_results": search_results_text},
        )
        full_output = [""]

        def on_output(text: str) -> None:
            full_output[0] = text

        manager = get_running_agent_manager()
        if manager is None:
            logger.warning("智能助手服务未运行，跳过搜索结果 AI 推荐")
            raise RuntimeError("智能助手服务未运行")
        await manager.run_background_prompt(
            message=prompt,
            session_prefix="__agent_search_recommend",
            output_callback=on_output,
            reply_mode=ReplyMode.CAPTURE_ONLY,
            allow_message_tools=False,
        )
        return full_output[0].strip()

    @staticmethod
    def _parse_recommend_response(ai_response: str) -> List[int]:
        """从模型文本中提取并规范化推荐索引数组。"""
        json_match = re.search(r"\[.*?]", ai_response, re.DOTALL)
        if not json_match:
            raise ValueError(f"无法从响应中提取JSON数组: {ai_response}")
        ai_indices = json.loads(json_match.group())
        if not isinstance(ai_indices, list):
            raise ValueError(f"AI返回格式错误: {ai_response}")
        return SearchRecommendOwner._normalize_ai_indices(ai_indices)

    def _recommend_prompt(self, items: List[str]) -> str:
        """组合用户偏好和候选资源，生成稳定的推荐输入。"""
        user_preference = (
            self.runtime_config.ai_recommend_user_preference or "Prefer high-quality resources with more seeders"
        )
        return f"User Preference: {user_preference}\n\nCandidate Resources:\n{chr(10).join(items)}"

    async def _generate_recommend_indices(
        self,
        *,
        filtered_indices: Optional[List[int]],
        results: List[Any],
    ) -> List[int]:
        """执行一次推荐请求并把局部索引恢复为原搜索结果索引。"""
        items, valid_indices = self._extract_recommend_items(
            filtered_indices=filtered_indices,
            results=results,
        )
        if not items:
            raise ValueError("没有可用于AI推荐的资源")
        ai_response = await self._invoke_recommend_llm(SearchRecommendOwner._recommend_prompt(self, items))
        if not ai_response:
            raise ValueError("AI推荐未返回结果")
        return self._restore_original_indices(
            ai_indices=SearchRecommendOwner._parse_recommend_response(ai_response),
            filtered_indices=filtered_indices,
            valid_indices=valid_indices,
            results_count=len(results),
        )

    async def _run_recommend(
        self,
        *,
        generation: int,
        request_hash: str,
        filtered_indices: Optional[List[int]],
        results: List[Any],
    ) -> None:
        """执行一个受代际栅栏保护的推荐任务并发布唯一终态。"""
        current_task = asyncio.current_task()
        try:
            original_indices = await SearchRecommendOwner._generate_recommend_indices(
                self,
                filtered_indices=filtered_indices,
                results=results,
            )
            if not recommend_coordinator.is_current(generation, request_hash):
                logger.info("AI推荐结果已过期，丢弃旧结果")
                return
            if not recommend_coordinator.publish_result(generation, original_indices):
                return
            await self.async_save_cache(
                {
                    "request_hash": request_hash,
                    "results": original_indices,
                },
                self._AI_INDICES_CACHE_KEY,
            )
            logger.info(f"AI推荐完成: {len(original_indices)}项")
        except asyncio.CancelledError:
            logger.info("AI推荐任务被取消")
        except Exception as err:
            logger.error(f"AI推荐任务失败: {err}")
            if recommend_coordinator.is_current(generation, request_hash):
                recommend_coordinator.publish_error(generation, str(err))
        finally:
            recommend_coordinator.finish(generation, current_task)

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

        request_hash = self._calculate_recommend_request_hash(
            filtered_indices,
            search_results_count,
            results,
        )
        generation, previous_task, started = recommend_coordinator.begin(request_hash)
        if not started:
            return
        if previous_task and not previous_task.done():
            previous_task.cancel()
        self.remove_cache(self._AI_INDICES_CACHE_KEY)

        task = get_task_registry().create(
            SearchRecommendOwner._run_recommend(
                self,
                generation=generation,
                request_hash=request_hash,
                filtered_indices=filtered_indices,
                results=results,
            ),
            owner="chain.search.ai_recommend",
        )
        if not recommend_coordinator.attach(generation, task):
            task.cancel()
