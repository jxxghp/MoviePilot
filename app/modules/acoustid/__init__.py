import asyncio
import json
import math
import shutil
import subprocess
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple, Union
from uuid import UUID

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.application.music.observation import (
    capture_music_recognition,
    music_recognition_diagnostics,
    music_request_timeout,
    music_wait_allowed,
    report_music_fingerprints,
    report_music_recognition,
)
from app.modules import _ModuleBase
from app.runtime.execution import run_in_threadpool
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.types import ModuleType, OtherModulesType


@dataclass(frozen=True, slots=True)
class _AcoustIdLookupPlan:
    """冻结 AcoustID 请求载荷，避免同步与异步入口产生参数漂移。"""

    url: str
    data: dict[str, Union[str, int]]


@dataclass(frozen=True, slots=True)
class _FingerprintCacheEntry:
    """文件指纹的有界缓存，保留候选和故障语义，避免仅缓存首个ID或永久空结果。"""

    recording_id: Optional[str]
    candidates: tuple[dict[str, Any], ...]
    expires_at: float
    status: str
    message: str


class AcoustIdModule(_ModuleBase):
    """通过 Chromaprint 本地指纹和 AcoustID API 识别 MusicBrainz Recording ID。"""

    _base_url = "https://api.acoustid.org/v2/lookup"
    # 退出码 3 表示解码期间出现非致命错误，结果仍须通过 JSON 内容校验。
    _usable_fpcalc_returncodes = frozenset({0, 3})
    _minimum_score = 0.9
    _request_interval = 0.34
    _fingerprint_timeout = 60
    _cache_max = 1024
    _request_lock = threading.Lock()
    _last_request_at = 0.0

    def __init__(self) -> None:
        """初始化 fpcalc 路径和进程内文件指纹识别缓存。"""
        super().__init__()
        self._fpcalc_path: Optional[str] = None
        self._cache: OrderedDict[tuple[str, int, int], _FingerprintCacheEntry] = OrderedDict()
        self._cache_lock = threading.Lock()

    @staticmethod
    def _resolve_fpcalc() -> Optional[str]:
        """在 PATH 中定位可执行的 fpcalc，返回其绝对路径，缺失则返回 None。

        shutil.which 在 POSIX 上已校验文件可执行，足以判定本地依赖是否就绪。
        """
        return shutil.which("fpcalc") or None

    def init_module(self) -> None:
        """定位 fpcalc 工具并清空可能过期的文件识别缓存。"""
        self._fpcalc_path = self._resolve_fpcalc()
        with self._cache_lock:
            self._cache.clear()
        if not self._fpcalc_path:
            logger.warning("AcoustID 已配置，但未找到 fpcalc，音频指纹识别将跳过")

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        """仅在配置 AcoustID 应用 API Key 后启用模块。"""
        return "ACOUSTID_API_KEY", True

    def stop(self) -> None:
        """停止模块并释放进程内文件识别缓存。"""
        with self._cache_lock:
            self._cache.clear()

    def test(self) -> Tuple[bool, str]:
        """检查 API Key、本地 fpcalc 依赖与 AcoustID API 的基础连通性。

        本地依赖检测独立于 init_module 的缓存快照，重新定位 fpcalc，确保即便
        模块初始化早于 fpcalc 安装，或运行期依赖被移除，测试也能如实反映本地
        依赖状态，而不是只校验网络连通性。
        """
        if not str(get_runtime_setting('ACOUSTID_API_KEY') or "").strip():
            return False, "AcoustID API Key 未配置"
        fpcalc_path = self._resolve_fpcalc()
        if not fpcalc_path:
            return False, "未找到 fpcalc，请先安装 Chromaprint"
        self._fpcalc_path = fpcalc_path
        response = RequestUtils(
            ua=get_runtime_setting('USER_AGENT'),
            proxies=get_runtime_setting('PROXY'),
            timeout=15,
        ).get_res(
            url=self._base_url,
            params={"client": get_runtime_setting('ACOUSTID_API_KEY'), "format": "json"},
        )
        if response is None:
            return False, "AcoustID 网络连接失败"
        try:
            if response.status_code >= 500:
                return False, f"AcoustID 服务异常：{response.status_code}"
            return True, ""
        finally:
            response.close()

    @staticmethod
    def get_name() -> str:
        """返回模块展示名称。"""
        return "AcoustID"

    @staticmethod
    def get_type() -> ModuleType:
        """返回模块所属的其它音乐能力类型。"""
        return ModuleType.Other

    @staticmethod
    def get_subtype() -> OtherModulesType:
        """返回 AcoustID 模块子类型。"""
        return OtherModulesType.AcoustId

    @staticmethod
    def get_priority() -> int:
        """返回音频指纹识别模块执行优先级。"""
        return 0

    def identify_music_by_fingerprint(self, path: Path) -> Optional[str]:
        """读取本地音频指纹并返回高置信匹配的 MusicBrainz Recording ID。"""
        with capture_music_recognition(seconds=90) as observed:
            report_music_fingerprints([])
            file_path = Path(path)
            if not self._fpcalc_path or not file_path.is_file():
                return None
            cache_key = self._file_cache_key(file_path)
            if cache_key:
                found, cached_id = self._get_cached(cache_key)
                if found:
                    return cached_id
            fingerprint = self._generate_fingerprint(file_path)
            recording_id = (
                self._lookup_recording_id(*fingerprint) if fingerprint else None
            )
            if fingerprint and observed.fingerprint_candidates:
                report_music_fingerprints([{**item, "duration": fingerprint[0]} for item in observed.fingerprint_candidates])
            if cache_key:
                self._set_cached(cache_key, recording_id, observed.fingerprint_candidates)
            return recording_id

    async def async_identify_music_by_fingerprint(
            self,
            path: Path,
    ) -> Optional[str]:
        """异步读取音频指纹并返回高置信匹配的 MusicBrainz Recording ID。"""
        with capture_music_recognition(seconds=90) as observed:
            report_music_fingerprints([])
            file_path = Path(path)
            if not self._fpcalc_path or not await run_in_threadpool(
                    Path.is_file,
                    file_path,
            ):
                return None
            cache_key = self._file_cache_key(file_path)
            if cache_key:
                found, cached_id = self._get_cached(cache_key)
                if found:
                    return cached_id
            fingerprint = await self._async_generate_fingerprint(file_path)
            recording_id = (
                await self._async_lookup_recording_id(*fingerprint)
                if fingerprint
                else None
            )
            if fingerprint and observed.fingerprint_candidates:
                report_music_fingerprints([{**item, "duration": fingerprint[0]} for item in observed.fingerprint_candidates])
            if cache_key:
                self._set_cached(cache_key, recording_id, observed.fingerprint_candidates)
            return recording_id

    @staticmethod
    def _file_cache_key(path: Path) -> Optional[tuple[str, int, int]]:
        """按规范路径、文件大小和修改时间构造可自动失效的缓存键。"""
        try:
            stat = path.stat()
            return str(path.resolve()), stat.st_size, stat.st_mtime_ns
        except OSError:
            return None

    def _get_cached(
            self,
            cache_key: tuple[str, int, int],
    ) -> tuple[bool, Optional[str]]:
        """读取并触摸文件识别 LRU 缓存，区分未缓存与已缓存未命中。"""
        with self._cache_lock:
            if cache_key not in self._cache:
                return False, None
            value = self._cache.pop(cache_key)
            if value.expires_at <= time.monotonic():
                return False, None
            self._cache[cache_key] = value
            report_music_fingerprints(list(value.candidates))
            if value.status in {"service_error", "budget_exhausted"}:
                report_music_recognition(value.status, value.message)
            return True, value.recording_id

    def _set_cached(
            self,
            cache_key: tuple[str, int, int],
            recording_id: Optional[str],
            candidates: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        """写入文件识别 LRU 缓存并淘汰最早使用的条目。"""
        diagnostics = music_recognition_diagnostics()
        status = str(diagnostics.get("status") or "not_found")
        ttl = 3600 if recording_id else 15 if status in {"service_error", "budget_exhausted"} else 300
        entry = _FingerprintCacheEntry(recording_id, tuple(dict(item) for item in candidates or []),
                                        time.monotonic() + ttl, status, str(diagnostics.get("message") or ""))
        with self._cache_lock:
            self._cache.pop(cache_key, None)
            self._cache[cache_key] = entry
            while len(self._cache) > self._cache_max:
                self._cache.popitem(last=False)

    def _generate_fingerprint(self, path: Path) -> Optional[tuple[int, str]]:
        """调用 fpcalc 生成 AcoustID 查询所需的完整时长和压缩指纹。"""
        timeout = music_request_timeout(claim=False, maximum=self._fingerprint_timeout)
        if timeout is None:
            return None
        try:
            result = subprocess.run(
                [self._fpcalc_path, "-json", str(path)],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as err:
            logger.warning(f"生成音频指纹失败：{path} - {err}")
            report_music_recognition("service_error", "音频指纹暂时无法生成，请检查音频解码支持")
            return None
        if result.returncode not in self._usable_fpcalc_returncodes:
            logger.warning(
                f"生成音频指纹失败：{path} - fpcalc 退出码 {result.returncode}"
            )
            report_music_recognition("service_error", "音频指纹解码失败")
            return None
        return self._parse_fingerprint_output(path, result.stdout)

    async def _async_generate_fingerprint(
            self,
            path: Path,
    ) -> Optional[tuple[int, str]]:
        """异步调用 fpcalc 生成 AcoustID 查询所需的指纹。"""
        timeout = music_request_timeout(claim=False, maximum=self._fingerprint_timeout)
        if timeout is None:
            return None
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                self._fpcalc_path,
                "-json",
                str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            if process and process.returncode is None:
                process.kill()
                await process.communicate()
            logger.warning(f"生成音频指纹超时：{path}")
            report_music_recognition("service_error", "音频指纹暂时无法生成，请检查音频解码支持")
            return None
        except asyncio.CancelledError:
            if process and process.returncode is None:
                process.kill()
                await process.communicate()
            raise
        except OSError as err:
            logger.warning(f"生成音频指纹失败：{path} - {err}")
            report_music_recognition("service_error", "音频指纹暂时无法生成，请检查音频解码支持")
            return None
        if process.returncode not in self._usable_fpcalc_returncodes:
            logger.warning(
                f"生成音频指纹失败：{path} - fpcalc 退出码 {process.returncode}"
            )
            report_music_recognition("service_error", "音频指纹解码失败")
            return None
        return self._parse_fingerprint_output(
            path,
            stdout.decode("utf-8", errors="replace"),
        )

    @staticmethod
    def _parse_fingerprint_output(
            path: Path,
            output: str,
    ) -> Optional[tuple[int, str]]:
        """解析 fpcalc JSON 输出中的音频时长和压缩指纹。"""
        try:
            payload = json.loads(output)
            duration = round(float(payload.get("duration") or 0))
            fingerprint = str(payload.get("fingerprint") or "").strip()
        except (AttributeError, OverflowError, TypeError, ValueError) as err:
            logger.warning(f"fpcalc 输出解析失败：{path} - {err}")
            report_music_recognition("service_error", "音频指纹暂时无法生成，请检查音频解码支持")
            return None
        if duration <= 0 or not fingerprint:
            logger.warning(f"fpcalc 未返回有效音频指纹：{path}")
            report_music_recognition("service_error", "音频指纹暂时无法生成，请检查音频解码支持")
            return None
        return duration, fingerprint

    @classmethod
    def _reserve_request_delay(cls) -> float:
        """为同步和异步 AcoustID 请求统一预留下一个发送时间。"""
        with cls._request_lock:
            now = time.monotonic()
            request_at = max(now, cls._last_request_at + cls._request_interval)
            if not music_wait_allowed(request_at - now):
                return -1
            cls._last_request_at = request_at
            return max(0.0, request_at - now)

    @classmethod
    def _wait_for_rate_limit(cls) -> bool:
        """同步等待 AcoustID 公共接口的已预留请求时间。"""
        delay = cls._reserve_request_delay()
        if delay < 0:
            return False
        if delay:
            time.sleep(delay)
        return True

    @classmethod
    async def _async_wait_for_rate_limit(cls) -> bool:
        """异步等待 AcoustID 公共接口的已预留请求时间。"""
        delay = cls._reserve_request_delay()
        if delay < 0:
            return False
        if delay:
            await asyncio.sleep(delay)
        return True

    @classmethod
    def _lookup_plan(
            cls,
            api_key: str,
            duration: int,
            fingerprint: str,
    ) -> Optional[_AcoustIdLookupPlan]:
        """校验 API Key 并构造同步与异步共用的指纹查询计划。"""
        normalized_key = str(api_key or "").strip()
        if not normalized_key:
            return None
        return _AcoustIdLookupPlan(
            url=cls._base_url,
            data={
                "client": normalized_key,
                "duration": duration,
                "fingerprint": fingerprint,
                "meta": "recordingids",
                "format": "json",
            },
        )

    @classmethod
    def _project_lookup_response(
            cls,
            status_code: Optional[int],
            payload: Any,
    ) -> Optional[str]:
        """按统一 HTTP 状态和 AcoustID 规则投影 Recording ID。"""
        if (status_code != 200 or not isinstance(payload, dict) or payload.get("status") != "ok"
                or not isinstance(payload.get("results", []), list)):
            report_music_recognition("service_error", "音频指纹服务暂时不可用")
            return None
        candidates = cls._select_recording_candidates(payload)
        report_music_fingerprints(candidates)
        return str(candidates[0]["recording_id"]) if candidates else None

    def _lookup_recording_id(
            self,
            duration: int,
            fingerprint: str,
    ) -> Optional[str]:
        """查询 AcoustID 指纹库并提取 MusicBrainz Recording ID。"""
        plan = self._lookup_plan(
            get_runtime_setting('ACOUSTID_API_KEY'),
            duration,
            fingerprint,
        )
        if not plan:
            return None
        if music_request_timeout(maximum=30) is None or self._wait_for_rate_limit() is False:
            return None
        timeout = music_request_timeout(claim=False, maximum=30)
        if timeout is None:
            return None
        response = RequestUtils(
            ua=get_runtime_setting('USER_AGENT'),
            proxies=get_runtime_setting('PROXY'),
            timeout=30,
        ).post_res(
            url=plan.url,
            data=plan.data,
            timeout=timeout,
        )
        if response is None:
            logger.warning("AcoustID 指纹查询失败：无响应")
            report_music_recognition("service_error", "音频指纹服务连接失败")
            return None
        try:
            if response.status_code != 200:
                logger.warning(f"AcoustID 指纹查询失败：HTTP {response.status_code}")
                report_music_recognition("service_error", "音频指纹服务暂时不可用")
                return None
            return self._project_lookup_response(
                response.status_code, response.json()
            )
        except (TypeError, ValueError) as err:
            logger.warning(f"AcoustID 响应解析失败：{err}")
            report_music_recognition("service_error", "音频指纹服务返回了无效响应")
            return None
        finally:
            response.close()

    async def _async_lookup_recording_id(
            self,
            duration: int,
            fingerprint: str,
    ) -> Optional[str]:
        """异步查询 AcoustID 指纹库并提取 MusicBrainz Recording ID。"""
        plan = self._lookup_plan(
            get_runtime_setting('ACOUSTID_API_KEY'),
            duration,
            fingerprint,
        )
        if not plan:
            return None
        if music_request_timeout(maximum=30) is None or await self._async_wait_for_rate_limit() is False:
            return None
        timeout = music_request_timeout(claim=False, maximum=30)
        if timeout is None:
            return None
        response = await AsyncRequestUtils(
            ua=get_runtime_setting('USER_AGENT'),
            proxies=get_runtime_setting('PROXY'),
            timeout=30,
        ).post_res(
            url=plan.url,
            data=plan.data,
            timeout=timeout,
        )
        if response is None:
            logger.warning("AcoustID 指纹查询失败：无响应")
            report_music_recognition("service_error", "音频指纹服务连接失败")
            return None
        try:
            if response.status_code != 200:
                logger.warning(f"AcoustID 指纹查询失败：HTTP {response.status_code}")
                report_music_recognition("service_error", "音频指纹服务暂时不可用")
                return None
            return self._project_lookup_response(
                response.status_code, response.json()
            )
        except (TypeError, ValueError) as err:
            logger.warning(f"AcoustID 响应解析失败：{err}")
            report_music_recognition("service_error", "音频指纹服务返回了无效响应")
            return None
        finally:
            await response.aclose()

    @classmethod
    def _select_recording_id(cls, payload: Any) -> Optional[str]:
        """兼容旧调用方的单ID视图，内部识别使用完整有界候选集合。"""
        candidates = cls._select_recording_candidates(payload)
        return str(candidates[0]["recording_id"]) if candidates else None

    @classmethod
    def _select_recording_candidates(cls, payload: Any) -> list[dict[str, Any]]:
        """按实际匹配分保留最多五个不同Recording ID，不用数组中的首个ID消除歧义。"""
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            return []
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        results = payload.get("results") or []
        if not isinstance(results, list):
            return []
        ranked = sorted(
            (item for item in results if isinstance(item, dict)),
            key=lambda item: cls._score(item.get("score")),
            reverse=True,
        )
        for item in ranked:
            score = cls._score(item.get("score"))
            if score < cls._minimum_score:
                break
            recordings = item.get("recordings") or []
            if not isinstance(recordings, list):
                continue
            for recording in recordings:
                recording_id = cls._normalize_recording_id(
                    recording.get("id") if isinstance(recording, dict) else None
                )
                if recording_id and recording_id not in seen:
                    seen.add(recording_id)
                    candidates.append({"recording_id": recording_id, "score": score})
                    if len(candidates) == 6:
                        return [{**candidate, "truncated": True} for candidate in candidates[:5]]
        return candidates

    @staticmethod
    def _score(value: Any) -> float:
        """将 AcoustID 匹配分安全转换为零到一之间的浮点数。"""
        try:
            score = float(value)
            return score if math.isfinite(score) and 0 <= score <= 1 else 0.0
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _normalize_recording_id(value: Any) -> Optional[str]:
        """校验并规范化 MusicBrainz UUID，拒绝异常外部响应进入详情路径。"""
        try:
            return str(UUID(str(value)))
        except (AttributeError, TypeError, ValueError):
            return None
