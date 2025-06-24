from typing import Optional, List, Tuple

from app.utils.singleton import Singleton


class StreamingPlatforms(metaclass=Singleton):
    """
    流媒体平台简称与全称。
    """
    STREAMING_PLATFORMS: List[Tuple[str, str]] = [
        ("AMZN", "Amazon"),
        ("NF", "Netflix"),
        ("ATVP", "Apple TV+"),
        ("iT", "iTunes"),
        ("DSNP", "Disney+"),
        ("HS", "Hotstar"),
        ("APPS", "Disney+ MENA"),
        ("PMTP", "Paramount+"),
        ("HMAX", "Max"),
        ("", "Max"),
        ("HULU", "Hulu"),
        ("MA", "Movies Anywhere"),
        ("BCORE", "Bravia Core"),
        ("MS", "Microsoft Store"),
        ("SHO", "Showtime"),
        ("STAN", "Stan"),
        ("PCOK", "Peacock"),
        ("SKST", "SkyShowtime"),
        ("NOW", "Now TV"),
        ("FXTL", "Foxtel Now"),
        ("BNGE", "Binge"),
        ("CRKL", "Crackle"),
        ("RKTN", "Rakuten TV"),
        ("ALL4", "All 4"),
        ("AS", "Adult Swim"),
        ("BRTB", "Brtb TV"),
        ("CNLP", "Canal+"),
        ("CRIT", "Criterion Channel"),
        ("DSCP", "Discovery+"),
        ("", "ESPN"),
        ("FOOD", "Food Network"),
        ("MUBI", "Mubi"),
        ("PLAY", "Google Play"),
        ("YT", "YouTube"),
        ("", "friDay"),
        ("", "KKTV"),
        ("", "ofiii"),
        ("", "LiTV"),
        ("", "MyVideo"),
        ("Hami", "Hami Video"),
        ("", "meWATCH"),
        ("CATCHPLAY", "CATCHPLAY+"),
        ("", "LINE TV"),
        ("VIU", "Viu"),
        ("IQ", ""),
        ("", "WeTV"),
        ("ABMA", "Abema"),
        ("ADN", ""),
        ("AT-X", ""),
        ("Baha", ""),
        ("BG", "B-Global"),
        ("CR", "Crunchyroll"),
        ("", "DMM"),
        ("FOD", ""),
        ("FUNi", "Funimation"),
        ("HIDI", "HIDIVE"),
        ("UNXT", "U-NEXT"),
    ]

    def __init__(self):
        """初始化流媒体平台匹配器"""
        self._lookup_cache = {}
        self._build_cache()

    def _build_cache(self) -> None:
        """
        构建查询缓存。
        """
        self._lookup_cache.clear()
        for short_name, full_name in self.STREAMING_PLATFORMS:
            canonical_name = full_name or short_name
            if not canonical_name:
                continue

            aliases = {short_name, full_name}
            for alias in aliases:
                if alias:
                    self._lookup_cache[alias.upper()] = canonical_name

    def get_streaming_platform_name(self, platform_code: str) -> Optional[str]:
        """
        根据流媒体平台简称或全称获取标准名称。
        """
        if platform_code is None:
            return None
        return self._lookup_cache.get(platform_code.upper())

    def is_streaming_platform(self, name: str) -> bool:
        """
        判断给定的字符串是否为已知的流媒体平台代码或名称。
        """
        if name is None:
            return False
        return name.upper() in self._lookup_cache
