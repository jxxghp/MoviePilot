"""站点查询应用服务的 ORM 投影契约测试。"""

from unittest.mock import Mock

from app.application.site.query import SiteQueryService
from app.db.models.siteuserdata import SiteUserData as SiteUserDataRecord
from app.schemas.site import SiteUserData


def test_userdata_latest_sync_projects_orm_record_to_dto() -> None:
    """同步站点用户数据查询应支持 SQLAlchemy ORM 记录。"""
    record = SiteUserDataRecord(
        domain="site.example",
        username="tester",
        userid="42",
        user_level="Power User",
        upload=1024.0,
        download=512.0,
        seeding=3.0,
        leeching=1.0,
        seeding_size=2048.0,
        leeching_size=256.0,
    )
    repository = Mock()
    repository.get_userdata_latest.return_value = [record]

    result = SiteQueryService(repository).userdata_latest_sync()

    assert isinstance(result[0], SiteUserData)
    assert result[0].domain == "site.example"
    assert result[0].userid == "42"
    assert result[0].upload == 1024
