"""
验证媒体库、播放条目与壁纸图片地址在返回前签名。
"""

from unittest.mock import Mock

from app.chain.mediaserver import MediaServerChain
from app.schemas.mediaserver import MediaServerLibrary, MediaServerPlayItem
from app.application.security.url import SecurityUtils


def _build_chain(result):
    """
    构造只带 run_module 的 MediaServerChain，避免单测初始化真实模块管理器。
    """
    chain = MediaServerChain.__new__(MediaServerChain)
    chain.run_module = Mock(return_value=result)
    return chain


def test_librarys_signs_image_fields() -> None:
    """
    媒体库接口返回前需要给 image 和 image_list 加签。
    """
    image = "http://192.168.1.50:8096/Items/lib/Images/Primary"
    image_list = [
        "http://192.168.1.50:32400/library/metadata/1/thumb/1",
    ]
    chain = _build_chain(
        [
            MediaServerLibrary(
                id="lib",
                image=image,
                image_list=image_list,
            )
        ]
    )

    result = chain.librarys(server="jellyfin")

    assert SecurityUtils.verify_signed_url(result[0].image) == image
    assert SecurityUtils.verify_signed_url(result[0].image_list[0]) == image_list[0]


def test_latest_signs_play_item_images() -> None:
    """
    最近入库接口返回前需要给条目图片加签。
    """
    image = "http://192.168.1.50:8096/Items/item/Images/Backdrop"
    chain = _build_chain([MediaServerPlayItem(id="item", image=image)])

    result = chain.latest(server="jellyfin")

    assert SecurityUtils.verify_signed_url(result[0].image) == image


def test_latest_wallpapers_signs_urls() -> None:
    """
    媒体服务器壁纸 URL 返回前也需要加签。
    """
    wallpaper = "http://192.168.1.50:8096/Items/item/Images/Backdrop"
    chain = _build_chain([wallpaper])

    result = chain.get_latest_wallpapers()

    assert SecurityUtils.verify_signed_url(result[0]) == wallpaper
