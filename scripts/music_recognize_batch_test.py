#!/usr/bin/env python3
"""多站点音乐种子批量识别测试工具。

用途：
1. fetch  - 用生产数据库中的站点 Cookie 抓取多个站点音乐分区种子标题
2. test   - 复用生产 MusicBrainz 识别入口，输出 CSV 报告与候选命中率汇总
3. 抓取结果落盘后可复用资源数据重测；识别仍会访问 MusicBrainz

约束：
- 生产库仅以只读 URI 模式打开，不做任何写入
- 识别仅调用 MusicBrainz 公共 API，遵守模块内置限流与退避重试
- 抓取仅请求各站点音乐分区浏览页若干页，对站点无压力

用法：
    .venv/bin/python scripts/music_recognize_batch_test.py --fetch          # 抓取并测试
    .venv/bin/python scripts/music_recognize_batch_test.py --fetch --sites ptsbao,springsunday
    .venv/bin/python scripts/music_recognize_batch_test.py                  # 用已保存标题离线重测
"""

import argparse
import csv
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

# 保证从仓库根目录外执行时也能导入 app 包
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 生产数据库（site 表中的站点 Cookie）与站点索引配置源文件目录
DB_PATH = Path.home() / "Documents" / "moviepilot" / "user.db"
INDEXER_DIR = Path.home() / "MPProjects" / "MoviePilot-Build" / "sites" / "private"

TITLES_FILE = ROOT / "config" / "temp" / "music_batch_titles.txt"
REPORT_FILE = ROOT / "config" / "temp" / "music_batch_report.csv"

# 各站点音乐分区浏览配置：yml 配置、浏览路径（{page} 由 SiteSpider 渲染）、每站抓取上限
# NexusPHP 站点统一用 torrents.php?cat=<音乐分类>，憨憨音乐专区走 special.php
SITES = [
    {"key": "hhanclub", "name": "憨憨", "yml": "hhanclub.yml",
     "browse": "special.php?page={page}", "limit": 30},
    {"key": "ptsbao", "name": "烧包乐园", "yml": "ptsbao.yml",
     "browse": "torrents.php?cat=414&page={page}", "limit": 30},
    {"key": "springsunday", "name": "春天", "yml": "springsunday.yml",
     "browse": "torrents.php?cat=508&page={page}", "limit": 30},
    {"key": "hdhome", "name": "家园", "yml": "hdhome.yml",
     "browse": "torrents.php?cat[]=439&cat[]=440&page={page}", "limit": 30},
    {"key": "btschool", "name": "学校", "yml": "btschool.yml",
     "browse": "torrents.php?cat=409&page={page}", "limit": 30},
    {"key": "0ff", "name": "自由农场", "yml": "0ff.yml",
     "browse": "torrents.php?cat=407&page={page}", "limit": 30},
    {"key": "hdfans", "name": "红豆饭", "yml": "hdfans.yml",
     "browse": "torrents.php?cat=406&page={page}", "limit": 30},
    {"key": "wintersakura", "name": "冬樱", "yml": "wintersakura.yml",
     "browse": "torrents.php?cat=408&page={page}", "limit": 30},
    {"key": "audiences", "name": "观众", "yml": "audiences.yml",
     "browse": "torrents.php?cat=408&page={page}", "limit": 30},
]


def load_site_credentials(domains: list[str]) -> dict[str, dict]:
    """只读打开生产库，按域名批量取出站点 Cookie 与 UA。"""
    from app.domain.site import extract_domain

    if not DB_PATH.exists():
        sys.exit(f"生产数据库不存在：{DB_PATH}")
    credentials: dict[str, dict] = {}
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        for domain in domains:
            row = con.execute(
                "SELECT cookie, ua, proxy FROM site WHERE domain = ?", (extract_domain(domain),)
            ).fetchone()
            if row and row[0]:
                credentials[domain] = {"cookie": row[0], "ua": row[1] or None, "proxy": bool(row[2])}
    finally:
        con.close()
    return credentials


def build_indexer(site: dict, credential: dict) -> dict:
    """加载站点索引配置并注入凭据，补充无关键词浏览所需的 browse 配置。

    索引 yml 没有 browse 节，这里按站点音乐分区构造浏览路径，
    列表与字段解析复用 yml 中 torrents 节的现有选择器，不改动配置文件。
    """
    with open(INDEXER_DIR / site["yml"], "r", encoding="utf-8") as f:
        indexer = yaml.safe_load(f)
    indexer["cookie"] = credential["cookie"]
    indexer["proxy"] = credential["proxy"]
    if credential["ua"]:
        indexer["ua"] = credential["ua"]
    indexer["browse"] = {"path": site["browse"]}
    return indexer


def fetch_site_titles(site: dict, indexer: dict, max_pages: int) -> list[tuple[str, str]]:
    """翻页抓取单个站点音乐分区种子标题，按标题去重并保留出现顺序。"""
    from app.modules.indexer.spider import SiteSpider
    from app.schemas.types import MediaType

    titles: list[tuple[str, str]] = []
    seen: set[str] = set()
    for page in range(max_pages):
        spider = SiteSpider(indexer=indexer, mtype=MediaType.MUSIC, page=page)
        torrents = spider.get_torrents()
        if spider.is_error:
            print(f"  [{site['name']}] 第 {page} 页请求失败，保留已有样本并停止翻页")
            break
        if not torrents:
            break
        for torrent in torrents:
            title = (torrent.get("title") or "").strip()
            if not title or title in seen:
                continue
            seen.add(title)
            titles.append((title, torrent.get("description") or ""))
        if len(titles) >= site["limit"]:
            break
    print(f"  [{site['name']}] 获取 {len(titles)} 条")
    return titles[:site["limit"]]


def fetch_titles(site_keys: list[str], max_pages: int) -> list[tuple[str, str, str]]:
    """按配置抓取多个站点音乐分区标题，返回 (站点名, 标题, 副标题) 列表。"""
    sites = [site for site in SITES if not site_keys or site["key"] in site_keys]
    unknown = set(site_keys) - {site["key"] for site in sites}
    if unknown:
        sys.exit(f"未知站点：{', '.join(sorted(unknown))}；可选：{', '.join(site['key'] for site in SITES)}")
    domains = []
    for site in sites:
        with open(INDEXER_DIR / site["yml"], "r", encoding="utf-8") as f:
            domain = urlsplit(yaml.safe_load(f).get("domain") or "").hostname or ""
        site["domain"] = domain
        domains.append(domain)
    credentials = load_site_credentials(domains)

    results: list[tuple[str, str, str]] = []
    for site in sites:
        credential = credentials.get(site["domain"])
        if not credential:
            print(f"  [{site['name']}] 未配置 Cookie，跳过")
            continue
        indexer = build_indexer(site, credential)
        for title, description in fetch_site_titles(site, indexer, max_pages):
            results.append((site["name"], title, description))
    return results


def recognize_one(module, title: str, description: str = "", music_type: str | None = None) -> dict:
    """复用生产模块的完整识别入口；没有远端 ID 的展示兜底不能计为命中。"""
    from app.domain.meta.metamusic import MetaMusic
    from app.schemas.types import MediaSource, MediaType

    row = {"title": title, "description": description, "requested_music_type": music_type or ""}
    try:
        meta = MetaMusic.parse_resource(title, description)
        row.update({
            "parsed_title": meta.title,
            "parsed_artists": " / ".join(meta.artists or []),
            "parsed_album": meta.album or "",
            "parsed_format": meta.audio_format or "",
        })
        matched = module.recognize_media(
            meta=meta, mtype=MediaType.MUSIC,
            media_source=MediaSource.MusicBrainz, music_type=music_type, cache=False,
        )
        if matched and matched.media_id:
            row.update({
                "status": "命中",
                "matched_title": matched.title,
                "matched_artists": " / ".join(matched.artists or []),
                "matched_album": matched.album or "",
                "matched_year": matched.year or "",
                "matched_music_type": matched.music_type,
                "media_id": matched.media_id,
            })
        else:
            row["status"] = "未命中" if meta.title else "解析失败"
    except Exception as err:  # pylint: disable=broad-exception-caught
        row.update({"status": "异常", "matched_title": type(err).__name__})
    return row


def run_batch(entries: list[tuple[str, str, str]], music_type: str | None = None) -> list[dict]:
    """批量执行识别并写出 CSV 报告，打印命中率汇总。"""
    from app.modules.musicbrainz import MusicBrainzModule

    module = MusicBrainzModule()
    rows = []
    for index, (site_name, title, description) in enumerate(entries, 1):
        row = recognize_one(module, title, description, music_type)
        row["site"] = site_name
        rows.append(row)
        if index % 20 == 0 or index == len(entries):
            print(f"识别进度 {index}/{len(entries)}")

    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "site", "title", "description", "requested_music_type", "status",
        "parsed_title", "parsed_artists", "parsed_album", "parsed_format",
        "matched_title", "matched_artists", "matched_album", "matched_year", "matched_music_type", "media_id",
    ]
    with open(REPORT_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    # 按状态汇总，输出命中率与失败分布
    summary: dict[str, int] = {}
    for row in rows:
        summary[row["status"]] = summary.get(row["status"], 0) + 1
    total = len(rows) or 1
    print(f"\n报告已写入：{REPORT_FILE}")
    for status, count in sorted(summary.items(), key=lambda kv: -kv[1]):
        print(f"  {status}: {count} ({count / total:.0%})")
    # 分站点命中率，便于定位特定站点标题格式问题
    print("分站点命中率：")
    for site_name in sorted({row["site"] for row in rows}):
        site_rows = [row for row in rows if row["site"] == site_name]
        hits = sum(1 for row in site_rows if row["status"].startswith("命中"))
        print(f"  {site_name}: {hits}/{len(site_rows)} ({hits / max(len(site_rows), 1):.0%})")
    return rows


def read_titles_file() -> list[tuple[str, str, str]]:
    """读取 TSV 主副标题，兼容历史二列 TSV 和纯标题文件。"""
    entries: list[tuple[str, str, str]] = []
    with TITLES_FILE.open(encoding="utf-8", newline="") as stream:
        for fields in csv.reader(stream, delimiter="\t"):
            if not fields or not any(fields):
                continue
            if len(fields) == 1:
                entries.append(("憨憨", fields[0], ""))
            else:
                entries.append((fields[0], fields[1], fields[2] if len(fields) > 2 else ""))
    return entries


def main() -> None:
    """在独立配置目录执行采样或重测，禁止识别缓存写入生产配置。"""
    parser = argparse.ArgumentParser(description="多站点音乐种子批量识别测试")
    parser.add_argument("--fetch", action="store_true", help="重新抓取种子标题（默认复用已保存列表）")
    parser.add_argument("--sites", default="", help="指定站点 key 逗号分隔，缺省抓取全部配置站点")
    parser.add_argument("--pages", type=int, default=3, help="每站最大翻页数")
    parser.add_argument("--fetch-only", action="store_true", help="只采样，不请求音乐元数据")
    parser.add_argument("--limit", type=int, default=0, help="最多识别多少条，0 表示全部")
    parser.add_argument("--music-type", choices=("recording", "album"), help="限定单曲或专辑，避免混合命中掩盖实体错误")
    args = parser.parse_args()
    if args.pages < 1 or args.limit < 0:
        parser.error("pages 必须大于 0，limit 不能小于 0")
    os.environ["CONFIG_DIR"] = str(ROOT / "config" / "temp" / "music-batch-runtime")

    if args.fetch or args.fetch_only or not TITLES_FILE.exists():
        site_keys = [key.strip() for key in args.sites.split(",") if key.strip()]
        entries = fetch_titles(site_keys, max_pages=args.pages)
        if not entries:
            sys.exit("未抓取到任何种子标题")
        TITLES_FILE.parent.mkdir(parents=True, exist_ok=True)
        with TITLES_FILE.open("w", encoding="utf-8", newline="") as stream:
            csv.writer(stream, delimiter="\t").writerows(entries)
        print(f"已保存 {len(entries)} 条标题到 {TITLES_FILE}")
    else:
        entries = read_titles_file()
        print(f"复用已保存的 {len(entries)} 条标题")

    if not args.fetch_only:
        run_batch(entries[:args.limit] if args.limit else entries, args.music_type)


if __name__ == "__main__":
    main()
