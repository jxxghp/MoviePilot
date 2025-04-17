from unittest import TestCase
from tests.cases.groups import release_group_cases
from app.core.meta.releasegroup import ReleaseGroupsMatcher

class MetaInfoTest(TestCase):
    def test_release_group(self):
        for info in release_group_cases:
            print(f"开始测试 {info.get('domain')}")
            for item in info.get('groups', []):
                release_group = ReleaseGroupsMatcher().match(item.get("title"))
                print(f"\tmatch release group {release_group}, should be: {item.get('group')}")
                self.assertEqual(item.get("group"), release_group)
            print(f"完成 {info.get('domain')}")
