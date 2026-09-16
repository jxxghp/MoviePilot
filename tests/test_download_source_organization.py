"""Isolated safety tests for the deployment-only source organization service."""

import importlib.util
import sys
import unittest
from enum import Enum
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch


class MediaType(str, Enum):
    MUSIC = "音乐"
    MOVIE = "电影"
    TV = "电视剧"


class SystemConfigKey(str, Enum):
    Downloaders = "Downloaders"


class MediaSource(str, Enum):
    MusicBrainz = "musicbrainz"


directory_module = ModuleType("app.application.directory")
directory_module.DirectoryHelper = Mock()
directory_module.validate_download_save_path = lambda value: str(value)
configuration_module = ModuleType("app.application.configuration")
configuration_module.get_configured_system_config = lambda: {
    SystemConfigKey.Downloaders: [{"name": "qb", "type": "qbittorrent"}]
}
metabase_module = ModuleType("app.domain.meta.metabase")
metabase_module.MetaBase = object
metamusic_module = ModuleType("app.domain.meta.metamusic")
metamusic_module.MetaMusic = NS(parse_query=lambda _value: NS())
metainfo_module = ModuleType("app.domain.metainfo")
metainfo_module.MetaInfo = lambda **_kwargs: NS()
types_module = ModuleType("app.schemas.types")
types_module.MediaSource = MediaSource
types_module.MediaType = MediaType
types_module.SystemConfigKey = SystemConfigKey
types_module.MUSIC_ARTIST_COLLECTION_CATEGORY = "Artist Collection"

with patch.dict(
    sys.modules,
    {
        "app.application.configuration": configuration_module,
        "app.application.directory": directory_module,
        "app.domain.meta.metabase": metabase_module,
        "app.domain.meta.metamusic": metamusic_module,
        "app.domain.metainfo": metainfo_module,
        "app.schemas.types": types_module,
    },
):
    spec = importlib.util.spec_from_file_location(
        "source_organization",
        Path(__file__).parent.parent / "app/application/download/organization.py",
    )
    organization = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(organization)


class SourceOrganizationTests(unittest.TestCase):
    def setUp(self):
        self.hash_value = "a" * 40
        self.request = NS(
            downloader=None,
            execute=False,
            mode="recognize",
            target_path=None,
            type_name="音乐",
            media_source=None,
            media_id=None,
            music_type="album",
            episode_group=None,
            media_category=None,
            smart_rename=True,
            expected_current_path=None,
            expected_target_path=None,
            expected_content_path=None,
            expected_root_name=None,
        )
        self.history = NS(
            downloader="qb",
            type="音乐",
            media_source=None,
            media_id=None,
            music_type=None,
            media_category=None,
            episode_group=None,
            torrent_name="Karen Mok - Loving Gaze 2002",
            torrent_description=None,
            title="含情脉脉",
        )
        self.torrent = NS(
            hash=self.hash_value,
            downloader="qb",
            title="Karen Mok - Loving Gaze 2002",
            save_path="/volume1/UT/Musics",
            content_path="/volume1/UT/Musics/Karen Mok - Loving Gaze 2002 FLAC",
            path=None,
        )
        self.media = NS(
            type=MediaType.MUSIC,
            album_type="Album",
            secondary_types=["Compilation"],
            classification_path=("Album", "Compilation"),
            album="含情脉脉",
            title="含情脉脉",
            album_artist="莫文蔚",
            artist="莫文蔚",
            year="2002",
            media_source="musicbrainz",
            media_id="release-group-id",
        )
        self.media_chain = Mock()
        self.media_chain.recognize_by_meta.return_value = self.media
        self.qbc = Mock()
        self.torrent_files = [NS(name="Karen Mok - Loving Gaze 2002 FLAC/01.flac")]
        module = NS(
            get_instance=lambda _name: NS(qbc=self.qbc),
            torrent_files=lambda **_kwargs: self.torrent_files,
        )
        self.chain = Mock()
        self.chain.download_history_repository.get_by_hash.return_value = self.history
        self.chain.list_torrents.return_value = [self.torrent]
        self.chain.modulemanager.get_running_module.return_value = module
        self.chain.update_torrent.return_value = {"save_path": True}
        directory_module.DirectoryHelper.return_value.get_download_dirs.return_value = [
            NS(
                storage="local",
                download_path="/volume1/UT/Musics",
                media_type="音乐",
                media_category="",
                download_type_folder=False,
                download_category_folder=True,
                priority=2,
            )
        ]
        directory_module.DirectoryHelper.return_value.classification_category_paths.return_value = (
            ("Album",),
            ("Album", "Compilation"),
            ("EP",),
            ("Action",),
        )
        directory_module.DirectoryHelper.return_value.resolve_media_category.side_effect = (
            lambda media: NS(path=getattr(media, "classification_path", ()))
        )

    def preview(self):
        return organization.organize_existing_source(
            self.hash_value,
            self.request,
            self.chain,
            self.media_chain,
        )

    def test_music_effective_classification_path_drives_source_directory(self):
        result = self.preview()
        self.assertEqual(result["category"], "Album/Compilation")
        self.assertEqual(result["secondary_categories"], ["Compilation"])
        self.assertEqual(
            result["target_save_path"],
            "/volume1/UT/Musics/Album/Compilation",
        )

    def test_music_category_falls_back_to_primary_type_without_classification(self):
        self.media.classification_path = ()
        result = self.preview()
        self.assertEqual(result["category"], "Album")
        self.assertEqual(result["target_save_path"], "/volume1/UT/Musics/Album")

    def test_artist_collection_uses_one_dedicated_source_category(self):
        self.request.music_type = "artist"
        self.history.music_type = "artist"
        self.media.music_type = "artist"
        self.media.name = "许嵩"
        self.media.classification_path = ("未分类",)
        self.torrent.title = "许嵩[2006-2022]录音室专辑合集"

        result = self.preview()

        self.assertEqual(result["category"], "Artist Collection")
        self.assertEqual(
            result["target_save_path"],
            "/volume1/UT/Musics/Artist Collection",
        )
        self.assertEqual(result["proposed_root_name"], "许嵩 - 艺术家合集")

    def test_preview_is_read_only_and_includes_qb_root_rename(self):
        result = self.preview()
        self.assertEqual(result["proposed_root_name"], "莫文蔚 - 含情脉脉 (2002)")
        self.assertTrue(result["rename_required"])
        self.chain.update_torrent.assert_not_called()
        self.qbc.torrents_rename_folder.assert_not_called()

    def test_execution_requires_exact_preview_replay(self):
        self.request.execute = True
        with self.assertRaisesRegex(ValueError, "重新预览"):
            self.preview()
        self.chain.update_torrent.assert_not_called()

    def test_confirmed_execution_uses_qb_rename_then_set_location(self):
        plan = self.preview()
        self.request.execute = True
        self.request.expected_current_path = plan["current_save_path"]
        self.request.expected_target_path = plan["target_save_path"]
        self.request.expected_content_path = plan["current_content_path"]
        self.request.expected_root_name = plan["proposed_root_name"]
        result = self.preview()
        self.assertTrue(result["renamed"])
        self.assertTrue(result["relocated"])
        self.qbc.torrents_rename_folder.assert_called_once_with(
            torrent_hash=self.hash_value,
            old_path="Karen Mok - Loving Gaze 2002 FLAC",
            new_path="莫文蔚 - 含情脉脉 (2002)",
        )
        self.chain.update_torrent.assert_called_once_with(
            hash_string=self.hash_value,
            downloader="qb",
            save_path="/volume1/UT/Musics/Album/Compilation",
        )

    def test_manual_directory_without_rename_skips_recognition(self):
        self.request.mode = "manual"
        self.request.target_path = "/volume1/UT/Musics/EP"
        self.request.smart_rename = False
        result = self.preview()
        self.assertFalse(result["recognized"])
        self.assertEqual(result["target_save_path"], "/volume1/UT/Musics/EP")
        self.media_chain.recognize_by_meta.assert_not_called()

    def test_failed_relocation_rolls_back_root_rename(self):
        plan = self.preview()
        self.request.execute = True
        self.request.expected_current_path = plan["current_save_path"]
        self.request.expected_target_path = plan["target_save_path"]
        self.request.expected_content_path = plan["current_content_path"]
        self.request.expected_root_name = plan["proposed_root_name"]
        self.chain.update_torrent.return_value = {"save_path": False}
        with self.assertRaisesRegex(ValueError, "已回滚"):
            self.preview()
        self.assertEqual(self.qbc.torrents_rename_folder.call_count, 2)
        self.qbc.torrents_rename_folder.assert_called_with(
            torrent_hash=self.hash_value,
            old_path="莫文蔚 - 含情脉脉 (2002)",
            new_path="Karen Mok - Loving Gaze 2002 FLAC",
        )

    def test_smart_rename_rejects_multi_root_tasks(self):
        self.torrent.content_path = self.torrent.save_path
        with self.assertRaisesRegex(ValueError, "单文件或散列文件"):
            self.preview()

    def test_dotted_folder_name_is_not_treated_as_a_file(self):
        folder = "Eagles.2011 - Hotel California SACD"
        self.torrent.content_path = f"/volume1/UT/Musics/{folder}"
        self.torrent_files = [NS(name=f"{folder}/01.dsf"), NS(name=f"{folder}/02.dsf")]
        result = self.preview()
        self.assertEqual(result["current_root_name"], folder)
        self.assertTrue(result["rename_supported"])

    def test_single_file_task_is_not_treated_as_a_folder(self):
        self.torrent.content_path = "/volume1/UT/Musics/Hotel California.dsf"
        self.torrent_files = [NS(name="Hotel California.dsf")]
        with self.assertRaisesRegex(ValueError, "单文件或散列文件"):
            self.preview()

    def test_changing_source_does_not_reuse_history_media_id(self):
        self.history.media_source = "other-source"
        self.history.media_id = "other-id"
        self.request.media_source = MediaSource.MusicBrainz
        self.preview()
        self.media_chain.recognize_media.assert_not_called()
        self.media_chain.recognize_by_meta.assert_called_once()

    def test_manual_category_must_exist_in_active_policy(self):
        self.request.type_name = "电影"
        self.request.media_category = "Unlisted"
        self.media.type = MediaType.MOVIE
        self.media.category = "Action"
        with self.assertRaisesRegex(ValueError, "不存在、已停用"):
            self.preview()

    def test_active_manual_category_overrides_recognized_music_category(self):
        self.request.media_category = "EP"
        result = self.preview()
        self.assertEqual(result["category"], "EP")
        self.assertEqual(result["target_save_path"], "/volume1/UT/Musics/EP")

    def test_windows_downloader_paths_are_parsed_by_path_style(self):
        folder = "Eagles.2011 - Hotel California SACD"
        self.torrent.save_path = r"D:\Downloads"
        self.torrent.content_path = rf"D:\Downloads\{folder}"
        self.torrent_files = [NS(name=f"{folder}/01.dsf"), NS(name=f"{folder}/02.dsf")]
        directory_module.DirectoryHelper.return_value.get_download_dirs.return_value[0].download_path = (
            "D:/Downloads"
        )
        result = self.preview()
        self.assertEqual(result["current_save_path"], "D:/Downloads")
        self.assertEqual(result["target_save_path"], "D:/Downloads/Album/Compilation")
        self.assertEqual(result["current_root_name"], folder)


if __name__ == "__main__":
    unittest.main()
