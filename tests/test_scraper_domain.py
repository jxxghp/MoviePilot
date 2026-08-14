import importlib

from app.domain.scraper import NfoReader


def test_nfo_reader_reads_values_after_scraper_consolidation(tmp_path):
    """合并后的 scraper 模块应继续读取单值和重复 NFO 元素。"""
    nfo_path = tmp_path / "movie.nfo"
    nfo_path.write_text(
        "<movie><title>MoviePilot</title><genre>剧情</genre><genre>音乐</genre></movie>",
        encoding="utf-8",
    )

    reader = NfoReader(nfo_path)

    assert reader.get_element_value("title") == "MoviePilot"
    assert [element.text for element in reader.get_elements("genre")] == [
        "剧情",
        "音乐",
    ]


def test_legacy_nfo_import_reuses_scraper_module():
    """旧插件 NFO 导入路径应映射到合并后的 scraper 模块。"""
    legacy = importlib.import_module("app.helper.nfo")
    canonical = importlib.import_module("app.domain.scraper")

    assert legacy is canonical
    assert legacy.NfoReader is NfoReader
