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

from app.runtime.log import logger

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
