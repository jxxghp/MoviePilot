from app.domain.meta import releasegroup as releasegroup_module
from app.domain.meta.releasegroup import ReleaseGroupsMatcher
from tests.cases.groups import release_group_cases


def test_release_group():
    for info in release_group_cases:
        print(f"开始测试 {info.get('domain')}")
        for item in info.get('groups', []):
            release_group = ReleaseGroupsMatcher().match(item.get("title"))
            print(f"\tmatch release group {release_group}, should be: {item.get('group')}")
            assert release_group == item.get("group")
        print(f"完成 {info.get('domain')}")


def test_custom_release_group_matches_multiple_adjacent_groups(monkeypatch):
    """自定义制作组共用分隔符时，应完整保留所有命中项。"""
    matcher = ReleaseGroupsMatcher()
    monkeypatch.setattr(
        releasegroup_module,
        "_release_groups_provider",
        lambda: ["VCB-Studio|hyakuhuyu|DMG|GM-Team"],
    )

    release_group = matcher.match(
        "[DMG&VCB-Studio] Youkoso Jitsuryoku Shijou Shugi no Kyoushitsu e"
    )

    assert release_group == "DMG@VCB-Studio"


def test_release_group_ignores_empty_title():
    """空标题不应进入制作组正则匹配。"""
    assert ReleaseGroupsMatcher().match("") == ""
