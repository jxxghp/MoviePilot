"""图片域名归一化与站点白名单快照，不缓存 DNS 安全判定。"""

import asyncio
import threading
from time import monotonic
from typing import Optional
from urllib.parse import urlsplit
from weakref import WeakValueDictionary

from app.application.site.query import SiteQueryService


def normalize_image_domain(address: Optional[str]) -> Optional[str]:
    """提取 HTTP(S) 主机及端口，保留协议以兼容 URL 白名单解析。"""
    address = (address or "").strip()
    if not address:
        return None
    try:
        parsed = urlsplit(address if "://" in address else f"https://{address}")
        if parsed.scheme in {"http", "https"} and parsed.hostname and parsed.username is None:
            return f"{parsed.scheme}://{parsed.netloc.lower()}"
    except ValueError:
        return None
    return None


class SiteImageDomainCache:
    """缓存站点图片域名，同 loop 合并查询，写入失效后禁止旧查询回填。"""

    def __init__(self, ttl: float = 60) -> None:
        """设置短期兜底有效期；锁只保护内存，不跨 await 持有线程锁。"""
        self._ttl = ttl
        self._lock = threading.Lock()
        self._inflight: WeakValueDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakValueDictionary()
        self._generation = 0
        self._source: Optional[SiteQueryService] = None
        self._domains: frozenset[str] = frozenset()
        self._expires = 0.0

    def invalidate(self) -> None:
        """提交后立即撤销旧域名；递增代际防止在途查询恢复已删除的授权。"""
        with self._lock:
            self._generation += 1
            self._expires = 0.0
            self._source = None
            self._domains = frozenset()

    async def get(self, source: SiteQueryService) -> frozenset[str]:
        """返回不可变快照，命中时不查库；配置服务更换后自动重建。"""
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._source is source and monotonic() < self._expires:
                return self._domains
            inflight = self._inflight.get(loop)
            if inflight is None:
                inflight = asyncio.Lock()
                self._inflight[loop] = inflight
        async with inflight:
            return await self._refresh(source)

    async def _refresh(self, source: SiteQueryService) -> frozenset[str]:
        """锁内复查并加载，失效与查询交错时丢弃旧结果后重新读取。"""
        while True:
            with self._lock:
                if self._source is source and monotonic() < self._expires:
                    return self._domains
                generation = self._generation
            sites = await source.list()
            domains = frozenset(
                domain
                for site in sites
                for address in (site.domain, site.url)
                if (domain := normalize_image_domain(address))
            )
            with self._lock:
                if generation != self._generation:
                    continue
                self._source = source
                self._domains = domains
                self._expires = monotonic() + self._ttl
                return domains


site_image_domains = SiteImageDomainCache()
