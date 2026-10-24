"""
音乐封面本地化：把外部音乐封面转换成浏览器可直接访问的本站地址。

Cover Art Archive 的封面请求会返回 307，跳转到 ``archive.org`` 存储节点。
在需要代理才能访问归档节点的网络环境中，浏览器直连必然失败，而容器内的图片
缓存后端可以通过 ``PROXY_HOST`` 取到图。因此音乐来源不再直接返回外部地址，
而是由后端取图后落到 nginx 静态根目录，前端拿到的就是可直连的本站地址。
"""

import os
import threading
from hashlib import sha256
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from app.adapters.network.http import RequestUtils
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting

# 本地化封面在静态根目录下的子路径，nginx 的 root 指向该根目录
_MUSIC_COVER_SUBDIR = Path("static") / "music"
# 文件名前缀，便于与其它静态资源区分并支持整目录清理
_MUSIC_COVER_PREFIX = "cover_"
# 文件名哈希长度，取哈希前缀即可在单目录内保持足够低的碰撞概率
_MUSIC_COVER_HASH_LENGTH = 24
# 同一个封面地址在进程内的初始化锁，避免并发首次访问重复下载
_localize_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _static_root() -> Optional[Path]:
    """
    返回 nginx 静态根目录。

    :return: 可写的静态根目录；无法定位时为 None
    """
    configured = str(os.environ.get("FRONTEND_PATH") or "").strip()
    candidates = [Path(configured)] if configured else []
    candidates.extend((Path("/public"), Path("/app/public")))
    for candidate in candidates:
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return None


def _cover_filename(url: str) -> str:
    """
    依据完整封面地址生成稳定的本地文件名。

    :param url: 外部封面地址
    :return: 带原图扩展名的文件名
    """
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}:
        suffix = ".jpg"
    digest = sha256(url.encode("utf-8")).hexdigest()[:_MUSIC_COVER_HASH_LENGTH]
    return f"{_MUSIC_COVER_PREFIX}{digest}{suffix}"


def _public_url(filename: str) -> str:
    """
    生成封面文件的对外访问路径。

    :param filename: 本地封面文件名
    :return: 以 / 开头的静态资源路径
    """
    return f"/{_MUSIC_COVER_SUBDIR.as_posix()}/{filename}"


def _store(root: Path, filename: str, content: bytes) -> Optional[str]:
    """
    原子写入封面文件并返回访问路径。

    :param root: 静态根目录
    :param filename: 目标文件名
    :param content: 已校验的图片内容
    :return: 写入成功时的访问路径，失败时为 None
    """
    target_dir = root / _MUSIC_COVER_SUBDIR
    target = target_dir / filename
    temp = target_dir / f".{filename}.tmp"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        temp.write_bytes(content)
        os.replace(temp, target)
    except OSError as err:
        logger.warning(f"音乐封面写入本地静态目录失败：{err}")
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return _public_url(filename)


def localize_music_cover(url: Optional[str], *, allow_network: bool = True) -> Optional[str]:
    """
    把外部音乐封面地址转换为本站可访问地址。

    已有本地文件时直接复用；否则通过统一图片缓存后端取图（遵循 ``PROXY_HOST``
    与图片内容校验），成功后保存到静态目录。任何失败都返回 None，由调用方决定
    回退策略，接口本身不会因此抛出异常。

    :param url: 外部封面地址，通常为 Cover Art Archive 地址
    :param allow_network: 是否允许在本地缺失时联网取图；批量列表场景可关闭以只复用已有文件
    :return: 本站封面访问路径；未本地化成功时为 None
    """
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    root = _static_root()
    if root is None:
        return None
    filename = _cover_filename(url)
    existing = root / _MUSIC_COVER_SUBDIR / filename
    try:
        if existing.is_file() and existing.stat().st_size > 0:
            return _public_url(filename)
    except OSError:
        return None
    if not allow_network:
        return None
    with _locks_guard:
        lock = _localize_locks.setdefault(filename, threading.Lock())
    if not lock.acquire(blocking=False):
        # 其它请求正在下载同一封面，本次直接放弃，由下次刷新复用结果
        return None
    try:
        try:
            if existing.is_file() and existing.stat().st_size > 0:
                return _public_url(filename)
        except OSError:
            return None
        from app.application.image import ImageHelper  # 延迟导入，避免启动期循环依赖

        try:
            fetched = ImageHelper().fetch_image(url, use_cache=True)
        except Exception as err:  # noqa: BLE001 - 取图失败不应打断推荐列表
            logger.warning(f"音乐封面取图失败：{url} - {err}")
            return None
        if not fetched:
            logger.debug(f"音乐封面暂不可用：{url}")
            return None
        return _store(root, filename, fetched)
    finally:
        lock.release()


def _release_id(url: str) -> Optional[str]:
    """
    从 Cover Art Archive 的 release 封面地址中取出 release 标识。

    :param url: 原始封面地址
    :return: release 标识；不是 release 封面地址时为 None
    """
    if "/release/" not in url:
        return None
    _, _, rest = url.partition("/release/")
    release_id = rest.split("/", 1)[0].strip()
    return release_id or None


def _lookup_release_group(release_id: str) -> Optional[str]:
    """
    查询 MusicBrainz，获取指定 release 所属的发行组标识。

    :param release_id: MusicBrainz release 标识
    :return: 发行组标识；查询失败时为 None
    """
    response = None
    try:
        response = RequestUtils(
            headers={
                "User-Agent": f"{get_runtime_setting('USER_AGENT')} (https://github.com/jxxghp/MoviePilot)",
                "Accept": "application/json",
            },
            proxies=get_runtime_setting('PROXY'),
            timeout=20,
        ).get_res(
            f"https://musicbrainz.org/ws/2/release/{release_id}",
            params={"inc": "release-groups", "fmt": "json"},
        )
        if response is None or response.status_code != 200:
            return None
        payload = response.json()
    except Exception as err:  # noqa: BLE001 - 兜底查询失败不影响主流程
        logger.debug(f"音乐封面发行组查询失败：{release_id} - {err}")
        return None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
    group = (payload or {}).get("release-group") or {}
    group_id = str(group.get("id") or "").strip()
    return group_id or None


def localize_music_cover_with_fallback(url: Optional[str]) -> Optional[str]:
    """
    依次尝试原地址与其发行组地址，返回第一个成功本地化的封面。

    部分专辑的某个 release 在归档节点缺失（返回 500），但同一发行组的其它 release
    仍有封面；改走 release-group 地址即可绕开单个损坏的归档文件。

    :param url: 外部封面地址
    :return: 本站封面路径；全部失败时为 None
    """
    localized = localize_music_cover(url)
    if localized:
        return localized
    source = str(url or "")
    release_id = _release_id(source)
    if not release_id:
        return None
    release_group_id = _lookup_release_group(release_id)
    if not release_group_id:
        return None
    prefix = source.partition("/release/")[0]
    return localize_music_cover(f"{prefix}/release-group/{release_group_id}/front-500")
