"""
将各通知实例的用户与管理员白名单迁移为统一渠道授权。

迁移与旧配置清理在同一事务中完成；投递接收人保留但以后不再隐含授予权限。
"""

import time
from uuid import NAMESPACE_URL, uuid4, uuid5

import sqlalchemy as sa
from alembic import op

revision = "d8a4c6e29170"
down_revision = "b7c1e3d5f9a2"
branch_labels = None
depends_on = None

# 冻结升级时的历史渠道键，不依赖未来宿主代码或模块是否已加载。
_CHANNELS = {
    "telegram": ("Telegram", "TELEGRAM_USERS", "TELEGRAM_ADMINS", "TELEGRAM_CHAT_ID", ("telegram_userid",)),
    "wechat": ("微信", "", "WECHAT_ADMINS", "WECHAT_BOT_CHAT_ID", ("wechat_userid",)),
    "wechatclawbot": (
        "微信ClawBot",
        "",
        "WECHATCLAWBOT_ADMINS",
        "WECHATCLAWBOT_DEFAULT_TARGET",
        ("wechatclawbot_userid",),
    ),
    "feishu": ("飞书", "", "FEISHU_ADMINS", "FEISHU_OPEN_ID", ("feishu_userid", "feishu_openid")),
    "qqbot": ("QQ", "", "QQBOT_ADMINS", "QQ_OPENID", ("qq_userid", "qq_openid")),
    "discord": ("Discord", "", "DISCORD_ADMINS", "", ("discord_userid",)),
    "slack": ("Slack", "", "SLACK_ADMINS", "", ("slack_userid",)),
    "vocechat": ("VoceChat", "", "VOCECHAT_ADMINS", "", ("vocechat_userid",)),
    "synologychat": ("SynologyChat", "", "SYNOLOGYCHAT_ADMINS", "", ("synologychat_userid",)),
}


def _ids(value: object) -> set[str]:
    """
    规整历史逗号分隔的完整平台标识。
    """
    return {item.strip() for item in str(value or "").split(",") if item.strip()}


def upgrade() -> None:
    """
    创建统一身份、迁移既有名单和账号绑定，然后原子移除旧名单键。
    """
    bind = op.get_bind()
    if "account_type" not in {column["name"] for column in sa.inspect(bind).get_columns("user")}:
        op.add_column("user", sa.Column("account_type", sa.String(16), nullable=False, server_default="local"))
    if "channelauthorization" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "channelauthorization",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("notification_id", sa.String(128), nullable=False),
            sa.Column("channel", sa.String(64), nullable=False),
            sa.Column("sender_id", sa.String(256), nullable=False),
            sa.Column("display_name", sa.String(256), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("role", sa.String(16), nullable=False),
            sa.Column("scopes", sa.JSON(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("user_identity", sa.String(32), nullable=False),
            sa.Column("linked_user_id", sa.Integer()),
            sa.Column("aliases", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.Column("origin", sa.String(16), nullable=False),
            sa.Column("reviewed_by", sa.Integer()),
        )
        op.create_index(
            "ux_channel_authorization_sender", "channelauthorization", ["notification_id", "sender_id"], unique=True
        )
    for name in ("agenttask", "agenttaskrun"):
        if "execution_authorization_id" not in {column["name"] for column in sa.inspect(bind).get_columns(name)}:
            op.add_column(name, sa.Column("execution_authorization_id", sa.String(32)))
    if "channelbinding" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "channelbinding",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("user_identity", sa.String(32), nullable=False),
            sa.Column("expires_at", sa.Float(), nullable=False),
        )
    metadata = sa.MetaData()
    users = sa.Table("user", metadata, autoload_with=bind)
    members = sa.Table("channelauthorization", metadata, autoload_with=bind)
    system = sa.Table("systemconfig", metadata, autoload_with=bind)
    row = bind.execute(sa.select(system).where(system.c.key == "Notifications")).mappings().first()
    if row is None or not isinstance(row["value"], list):
        return
    configs = []
    accounts = list(bind.execute(sa.select(users).where(users.c.account_type == "local")).mappings())
    now = time.time()
    for raw in row["value"]:
        if not isinstance(raw, dict):
            configs.append(raw)
            continue
        config = dict(raw)
        config["id"] = str(config.get("id") or uuid4())
        definition = _CHANNELS.get(config.get("type"))
        if definition is None:
            configs.append(config)
            continue
        channel, users_key, admins_key, primary_key, bindings = definition
        values = dict(config.get("config") or {})
        roles = dict.fromkeys(_ids(values.get(users_key)), "member")
        admins = _ids(values.get(admins_key))
        if primary_key and (config["type"] != "wechat" or values.get("WECHAT_MODE", "app") == "bot"):
            admins.update(_ids(values.get(primary_key)))
        for account in accounts:
            for key in bindings:
                bound = str((account["settings"] or {}).get(key) or "").strip()
                if bound and (not values.get(users_key) or bound in roles or bound in admins):
                    roles.setdefault(bound, "member")
        roles.update(dict.fromkeys(admins, "admin"))
        for sender_id, role in sorted(roles.items()):
            existing = bind.execute(
                sa.select(members.c.id).where(
                    members.c.notification_id == config["id"], members.c.sender_id == sender_id
                )
            ).first()
            if existing is not None:
                continue
            identity = uuid5(NAMESPACE_URL, f"moviepilot:channel:{config['id']}:{sender_id}").hex
            linked = [
                account
                for account in accounts
                if any(str((account["settings"] or {}).get(key) or "") == sender_id for key in bindings)
            ]
            if len(linked) == 1:
                user_id = linked[0]["id"]
                user_identity = linked[0]["notification_identity"]
            else:
                user_identity = uuid4().hex
                result = bind.execute(
                    users.insert().values(
                        name=f"channel_{identity}",
                        account_type="channel",
                        is_active=True,
                        is_superuser=False,
                        notification_identity=user_identity,
                        hashed_password=None,
                        permissions={},
                        settings={bindings[0]: sender_id},
                        is_otp=False,
                    )
                )
                user_id = result.inserted_primary_key[0]
            bind.execute(
                members.insert().values(
                    id=identity,
                    notification_id=config["id"],
                    channel=channel,
                    sender_id=sender_id,
                    display_name=linked[0]["name"] if len(linked) == 1 else sender_id,
                    status="pending" if len(linked) > 1 else "approved",
                    role=role,
                    scopes=["search", "subscribe", "download", "schedule"],
                    user_id=user_id,
                    user_identity=user_identity,
                    linked_user_id=user_id if len(linked) == 1 and role != "admin" else None,
                    aliases=[],
                    created_at=now,
                    updated_at=now,
                    origin="legacy",
                )
            )
            tasks = sa.Table("agenttask", sa.MetaData(), autoload_with=bind)
            if {"channel", "source", "user_id", "execution_user_id", "execution_user_identity"}.issubset(
                tasks.c.keys()
            ):
                bind.execute(
                    tasks.update()
                    .where(
                        tasks.c.channel == channel,
                        tasks.c.source == config.get("name"),
                        tasks.c.user_id == sender_id,
                        tasks.c.execution_user_id == user_id,
                        tasks.c.execution_user_identity == user_identity,
                        tasks.c.execution_authorization_id.is_(None),
                    )
                    .values(user_id=f"channel:{identity}", execution_authorization_id=identity)
                )
        values.pop(users_key, None)
        values.pop(admins_key, None)
        config["config"] = values
        configs.append(config)
    bind.execute(system.update().where(system.c.id == row["id"]).values(value=configs))


def downgrade() -> None:
    """
    拒绝丢失已迁移授权及业务归属的自动降级，回退应恢复升级前完整数据库备份。
    """
    raise RuntimeError("渠道授权迁移包含持久化身份与权限，请使用升级前数据库备份回退")
