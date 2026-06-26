"""过滤器内置规则定义。"""

from typing import Dict

# 内置规则只在这里维护一份，便于过滤模块和 Agent 工具共享同一套事实来源。
BUILTIN_RULE_SET: Dict[str, dict] = {
    # 蓝光原盘
    "BLU": {
        "include": [
            r"(?i)(\bBlu-?Ray\b.*\b(?:VC-?1|AVC|MPEG-?2)\b|\b(?:UHD|4K|2160p)\b(?:.*Blu-?Ray)?.*\b(?:HEVC|H\.?265)\b|\bBlu-?Ray\b.*\b(?:UHD|4K|2160p)\b.*\b(?:HEVC|H\.?265)\b|\b(?:COMPLETE|FULL)\b.*\b(?:(?:UHD|4K|2160p)\b.*)?Blu-?Ray\b|\b(BD25|BD50|BD66|BD100|BDMV|MiniBD)\b)"
        ],
        "exclude": [
            r"(?i)(\b[XH]\.?264\b|\b[XH]\.?265\b|\bWEB-?DL\b|\bWEB-?RIP\b|\bHDTV(?:RIP)?\b|\bREMUX\b|\bBDRip\b|\bBRRip\b|\bHDRip\b|\bENCODE\b|\b(?<!WEB-|HDTV)RIP\b)"
        ],
    },
    # 4K
    "4K": {
        "include": [r"4k|2160p|x2160"],
        "exclude": [],
    },
    # 1080P
    "1080P": {
        "include": [r"1080[pi]|x1080"],
        "exclude": [],
    },
    # 720P
    "720P": {
        "include": [r"720[pi]|x720"],
        "exclude": [],
    },
    # 中字
    "CNSUB": {
        "include": [
            r"[中国國繁简](/|\s|\\|\|)?[繁简英粤]|[英简繁](/|\s|\\|\|)?[中繁简]"
            r"|繁體|简体|[中国國][字配]|国语|國語|中文|中字|简日|繁日|简繁|繁体"
            r"|([\s,.-\[])(chs|cht)(|[\s,.-\]])"
            r"|(?<![a-z0-9])(gb|big5)(?![a-z0-9])"
        ],
        "exclude": [],
        "tmdb": {
            "original_language": "zh,cn",
        },
    },
    # 官种
    "GZ": {
        "include": [r"官方", r"官种", r"官组"],
        "match": ["labels"],
    },
    # 特效字幕
    "SPECSUB": {
        "include": [r"特效"],
        "exclude": [],
    },
    # BluRay
    "BLURAY": {
        "include": [r"Blu-?Ray"],
        "exclude": [],
    },
    # UHD
    "UHD": {
        "include": [r"UHD|UltraHD"],
        "exclude": [],
    },
    # H265
    "H265": {
        "include": [r"[Hx].?265|HEVC"],
        "exclude": [],
    },
    # H264
    "H264": {
        "include": [r"[Hx].?264|AVC"],
        "exclude": [],
    },
    # 杜比视界
    "DOLBY": {
        "include": [r"Dolby[\s.]+Vision|DOVI|[\s.]+DV[\s.]+|杜比视界"],
        "exclude": [],
    },
    # 杜比全景声
    "ATMOS": {
        "include": [r"Dolby[\s.+]+Atmos|Atmos|杜比全景[声聲]"],
        "exclude": [],
    },
    # HDR
    "HDR": {
        "include": [r"[\s.]+HDR[\s.]+|HDR10|HDR10\+|HDRVivid"],
        "exclude": [],
    },
    # SDR
    "SDR": {
        "include": [r"[\s.]+SDR[\s.]+"],
        "exclude": [],
    },
    # 重编码
    "REMUX": {
        "include": [r"REMUX"],
        "exclude": [],
    },
    # WEB-DL
    "WEBDL": {
        "include": [r"WEB-?DL|WEB-?RIP"],
        "exclude": [],
    },
    # 免费
    "FREE": {
        "downloadvolumefactor": 0,
    },
    # 国语配音
    "CNVOI": {
        "include": [r"[国國][语語]配音|[国國]配|[国國][语語]"],
        "exclude": [],
        "tmdb": {
            "original_language": "zh",
        },
    },
    # 粤语配音
    "HKVOI": {
        "include": [r"粤语配音|粤语"],
        "exclude": [],
    },
    # 60FPS
    "60FPS": {
        "include": [r"60fps|60帧"],
        "exclude": [],
    },
    # 3D
    "3D": {
        "include": [r"3D"],
        "exclude": [],
    },
}
