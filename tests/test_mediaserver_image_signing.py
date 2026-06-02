import sys
import unittest
from unittest.mock import Mock

for _module_name in (
    "app.chain.mediaserver",
    "app.db.models",
    "app.db.user_oper",
    "app.helper.message",
    "app.utils.crypto",
):
    if _module_name in sys.modules and not hasattr(
        sys.modules[_module_name], "__file__"
    ):
        del sys.modules[_module_name]

from app.chain.mediaserver import MediaServerChain
from app.schemas import MediaServerLibrary, MediaServerPlayItem
from app.utils.security import SecurityUtils


class MediaServerImageSigningTest(unittest.TestCase):
    @staticmethod
    def _build_chain(result):
        """
        构造只带 run_module 的 MediaServerChain，避免单测初始化真实模块管理器。
        """
        chain = MediaServerChain.__new__(MediaServerChain)
        chain.run_module = Mock(return_value=result)
        return chain

    def test_librarys_signs_image_fields(self):
        """
        媒体库接口返回前需要给 image 和 image_list 加签。
        """
        image = "http://192.168.1.50:8096/Items/lib/Images/Primary"
        image_list = [
            "http://192.168.1.50:32400/library/metadata/1/thumb/1",
        ]
        chain = self._build_chain(
            [
                MediaServerLibrary(
                    id="lib",
                    image=image,
                    image_list=image_list,
                )
            ]
        )

        result = chain.librarys(server="jellyfin")

        self.assertEqual(SecurityUtils.verify_signed_url(result[0].image), image)
        self.assertEqual(
            SecurityUtils.verify_signed_url(result[0].image_list[0]),
            image_list[0],
        )

    def test_latest_signs_play_item_images(self):
        """
        最近入库接口返回前需要给条目图片加签。
        """
        image = "http://192.168.1.50:8096/Items/item/Images/Backdrop"
        chain = self._build_chain([MediaServerPlayItem(id="item", image=image)])

        result = chain.latest(server="jellyfin")

        self.assertEqual(SecurityUtils.verify_signed_url(result[0].image), image)

    def test_latest_wallpapers_signs_urls(self):
        """
        媒体服务器壁纸 URL 返回前也需要加签。
        """
        wallpaper = "http://192.168.1.50:8096/Items/item/Images/Backdrop"
        chain = self._build_chain([wallpaper])

        result = chain.get_latest_wallpapers()

        self.assertEqual(SecurityUtils.verify_signed_url(result[0]), wallpaper)
