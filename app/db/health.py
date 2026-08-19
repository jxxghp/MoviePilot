"""数据库技术边界的连通性探测实现。"""

from sqlalchemy import text

from app.db.session import SessionFactory


def probe_database() -> str | None:
    """执行最小数据库查询，成功返回空值，失败返回错误文本。"""
    session = SessionFactory()
    try:
        session.execute(text("SELECT 1"))
    except Exception as err:  # noqa: BLE001  探测需要把驱动错误转为诊断文本
        return str(err)
    finally:
        session.close()
    return None
