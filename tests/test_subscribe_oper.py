from app.db.models.subscribehistory import SubscribeHistory
from app.db.subscribe_oper import SubscribeOper


def test_add_history_converts_boolean_integer_flags(monkeypatch):
    """
    写入订阅历史前应把布尔开关转为整型，兼容 PostgreSQL 的严格类型检查。
    """
    captured = {}

    def fake_create(self, _db):
        """
        截获待写入模型，避免测试依赖具体数据库方言的类型宽松行为。
        """
        captured.update({
            "id": self.id,
            "best_version": self.best_version,
            "best_version_full": self.best_version_full,
            "search_imdbid": self.search_imdbid,
        })

    monkeypatch.setattr(SubscribeHistory, "create", fake_create)

    SubscribeOper().add_history(
        id=100,
        name="Test Movie",
        type="电影",
        best_version=False,
        best_version_full=True,
        search_imdbid=False,
        unknown_field=True,
    )

    assert captured == {
        "id": None,
        "best_version": 0,
        "best_version_full": 1,
        "search_imdbid": 0,
    }
