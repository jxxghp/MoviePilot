"""插件来源身份的数据访问原语。"""

from typing import cast

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.base import DbOper, execute_dml
from app.db.models.pluginidentity import PluginIdentity


class PluginIdentityOper(DbOper):
    """在调用方 Session 中查询并条件暂存插件来源身份。"""

    def get_by_plugin_id(self, plugin_id: str) -> PluginIdentity | None:
        """按规范化物理插件 ID 查询唯一身份。"""
        return self._execute_sync_query(
            lambda session: cast(
                PluginIdentity | None,
                session.execute(
                    select(PluginIdentity).where(
                        PluginIdentity.normalized_plugin_id == plugin_id
                    )
                ).scalar_one_or_none(),
            )
        )

    def stage_create(self, identity: PluginIdentity) -> None:
        """暂存首次身份并立即暴露数据库唯一键竞争。"""
        def stage(session: Session) -> None:
            """加入并 flush 当前调用方事务。"""
            session.add(identity)
            session.flush()

        self._execute_sync_write(stage)

    def stage_replace(
        self,
        identity: PluginIdentity,
        *,
        expected_revision: int,
    ) -> bool:
        """仅在当前 revision 匹配时替换整份审计事实。"""
        values = {
            column.name: getattr(identity, column.name)
            for column in PluginIdentity.__table__.columns
            if column.name != "id"
        }
        return bool(
            self._execute_sync_write(
                lambda session: execute_dml(
                    session,
                    update(PluginIdentity)
                    .where(
                        PluginIdentity.normalized_plugin_id
                        == identity.normalized_plugin_id,
                        PluginIdentity.revision == expected_revision,
                    )
                    .values(**values),
                    execution_options={"synchronize_session": False},
                )
            )
        )
