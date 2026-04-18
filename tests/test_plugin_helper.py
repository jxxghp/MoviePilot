from unittest import TestCase


class PluginHelperTest(TestCase):

    def test_sanitize_repo_url_for_statistic_keeps_remote_url(self):
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            self.skipTest(f"missing dependency: {exc}")
        repo_url = "https://github.com/InfinityPacer/MoviePilot-Plugins"
        self.assertEqual(repo_url, PluginHelper.sanitize_repo_url_for_statistic(repo_url))

    def test_sanitize_repo_url_for_statistic_strips_local_path(self):
        try:
            from app.helper.plugin import PluginHelper
        except ModuleNotFoundError as exc:
            self.skipTest(f"missing dependency: {exc}")
        repo_url = "local://TestPlugin?path=/Users/InfinityPacer/GitHub/MoviePilot/MoviePilot-Plugins&version=v2"
        self.assertEqual(
            "local://TestPlugin?version=v2",
            PluginHelper.sanitize_repo_url_for_statistic(repo_url)
        )
