#!/usr/bin/env python3
"""多站点音乐种子批量识别测试工具。

用途：
1. fetch  - 用生产数据库中的站点 Cookie 抓取多个站点音乐分区种子标题
2. test   - 对标题批量执行音乐识别链路（与识别测试页相同），输出 CSV 报告与命中率汇总
3. 抓取结果落盘后可离线重跑 test，用于优化识别程序前后对比

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
import sqlite3
import sys
from pathlib import Path

import yaml

# 保证从仓库根目录外执行时也能导入 app 包
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 生产数据库（site 表中的站点 Cookie）与站点索引配置源文件目录
DB_PATH = Path.home() / "Documents" / "MoviePilot" / "user.db"
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
    if not DB_PATH.exists():
        sys.exit(f"生产数据库不存在：{DB_PATH}")
    credentials: dict[str, dict] = {}
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        for domain in domains:
            row = con.execute(
                "SELECT cookie, ua FROM site WHERE domain = ?", (domain,)
            ).fetchone()
            if row and row[0]:
                credentials[domain] = {"cookie": row[0], "ua": row[1] or None}
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
    if credential["ua"]:
        indexer["ua"] = credential["ua"]
    indexer["browse"] = {"path": site["browse"]}
    return indexer


def fetch_site_titles(site: dict, indexer: dict, max_pages: int) -> list[str]:
    """翻页抓取单个站点音乐分区种子标题，按标题去重并保留出现顺序。"""
    from app.modules.indexer.spider import SiteSpider
    from app.schemas.types import MediaType

    titles: list[str] = []
    seen: set[str] = set()
    for page in range(max_pages):
        spider = SiteSpider(indexer=indexer, mtype=MediaType.MUSIC, page=page)
        torrents = spider.get_torrents()
        if spider.is_error:
            print(f"  [{site['name']}] 第 {page} 页请求失败（Cookie 可能失效或站点不可达），跳过该站点")
            return []
        if not torrents:
            break
        for torrent in torrents:
            title = (torrent.get("title") or "").strip()
            if not title or title in seen:
                continue
            seen.add(title)
            titles.append(title)
        if len(titles) >= site["limit"]:
            break
    print(f"  [{site['name']}] 获取 {len(titles)} 条")
    return titles[:site["limit"]]


def fetch_titles(site_keys: list[str], max_pages: int) -> list[tuple[str, str]]:
    """按配置抓取多个站点音乐分区标题，返回 (站点名, 标题) 列表。"""
    sites = [site for site in SITES if not site_keys or site["key"] in site_keys]
    unknown = set(site_keys) - {site["key"] for site in sites}
    if unknown:
        sys.exit(f"未知站点：{', '.join(sorted(unknown))}；可选：{', '.join(site['key'] for site in SITES)}")
    domains = []
    for site in sites:
        with open(INDEXER_DIR / site["yml"], "r", encoding="utf-8") as f:
            domain = (yaml.safe_load(f).get("domain") or "").replace("https://", "").replace("http://", "").rstrip("/")
        site["domain"] = domain
        domains.append(domain)
    credentials = load_site_credentials(domains)

    results: list[tuple[str, str]] = []
    for site in sites:
        credential = credentials.get(site["domain"])
        if not credential:
            print(f"  [{site['name']}] 未配置 Cookie，跳过")
            continue
        indexer = build_indexer(site, credential)
        for title in fetch_site_titles(site, indexer, max_pages):
            results.append((site["name"], title))
    return results


def recognize_one(module, title: str) -> dict:
    """对单条标题执行与识别测试页相同的解析+识别链路，并给出失败归因。"""
    from app.core.meta import MetaMusic

    row = {"title": title}
    try:
        meta = MetaMusic.parse_query(title)
        row.update({
            "parsed_title": meta.title,
            "parsed_artists": " / ".join(meta.artists or []),
            "parsed_format": meta.audio_format or "",
        })
        # 拆开检索与候选挑选两步，便于区分零命中与比对失配
        candidates = module._search_recordings(meta, limit=10)
        matched = module._select_candidate(meta, candidates, source="musicbrainz")
        hit_label = "命中"
        albums: list = []
        # 与正式链路一致：专辑挑选要求艺术家命中，无艺术家线索时跳过专辑回退检索
        if not matched and meta.artists:
            albums = module._search_albums(meta, limit=10)
            matched = module._select_album_candidate(meta, albums)
            hit_label = "命中(专辑)"
        if matched:
            row.update({
                "status": hit_label,
                "matched_title": matched.title,
                "matched_artists": " / ".join(matched.artists or []),
                "matched_album": matched.album or "",
                "matched_year": matched.year or "",
                "media_id": matched.media_id,
            })
        elif candidates:
            # 有候选但全部比对失配，列出最接近的候选方便归因
            top = candidates[0]
            row.update({
                "status": "候选比对失配",
                "matched_title": f"{top.title} | {' / '.join(top.artists or [])}",
            })
        elif albums:
            row.update({
                "status": "专辑候选失配",
                "matched_title": f"{albums[0].title} | {' / '.join(albums[0].artists or [])}",
            })
        elif not meta.title:
            row.update({"status": "解析失败"})
        else:
            row.update({"status": "检索零命中"})
    except Exception as err:  # pylint: disable=broad-except
        row.update({"status": "异常", "matched_title": str(err)[:200]})
    return row


def run_batch(entries: list[tuple[str, str]]) -> list[dict]:
    """批量执行识别并写出 CSV 报告，打印命中率汇总。"""
    from app.modules.musicbrainz import MusicBrainzModule

    module = MusicBrainzModule()
    module.init_module()
    rows = []
    for index, (site_name, title) in enumerate(entries, 1):
        row = recognize_one(module, title)
        row["site"] = site_name
        rows.append(row)
        if index % 20 == 0 or index == len(entries):
            print(f"识别进度 {index}/{len(entries)}")

    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "site", "title", "status", "parsed_title", "parsed_artists", "parsed_format",
        "matched_title", "matched_artists", "matched_album", "matched_year", "media_id",
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


def read_titles_file() -> list[tuple[str, str]]:
    """读取落盘标题，兼容旧版纯标题格式（无站点前缀记为「憨憨」）。"""
    entries: list[tuple[str, str]] = []
    for line in TITLES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if "\t" in line:
            site_name, title = line.split("\t", 1)
        else:
            site_name, title = "憨憨", line
        entries.append((site_name, title))
    return entries


def main() -> None:
    parser = argparse.ArgumentParser(description="多站点音乐种子批量识别测试")
    parser.add_argument("--fetch", action="store_true", help="重新抓取种子标题（默认复用已保存列表）")
    parser.add_argument("--sites", default="", help="指定站点 key 逗号分隔，缺省抓取全部配置站点")
    parser.add_argument("--pages", type=int, default=3, help="每站最大翻页数")
    args = parser.parse_args()

    if args.fetch or not TITLES_FILE.exists():
        site_keys = [key.strip() for key in args.sites.split(",") if key.strip()]
        entries = fetch_titles(site_keys, max_pages=args.pages)
        if not entries:
            sys.exit("未抓取到任何种子标题")
        TITLES_FILE.parent.mkdir(parents=True, exist_ok=True)
        TITLES_FILE.write_text(
            "\n".join(f"{site_name}\t{title}" for site_name, title in entries),
            encoding="utf-8",
        )
        print(f"已保存 {len(entries)} 条标题到 {TITLES_FILE}")
    else:
        entries = read_titles_file()
        print(f"复用已保存的 {len(entries)} 条标题")

    run_batch(entries)


if __name__ == "__main__":
    main()
