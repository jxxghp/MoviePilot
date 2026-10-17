"""
验证 PostgreSQL TCP 与 Unix Socket 连接 URL 构造。
"""

from app.runtime.config import Settings


def test_postgresql_tcp_url_keeps_host_and_port() -> None:
    """
    PostgreSQL TCP 连接保留配置的主机和端口。
    """
    settings = Settings(
        DB_POSTGRESQL_HOST="db",
        DB_POSTGRESQL_PORT="5433",
        DB_POSTGRESQL_DATABASE="moviepilot",
        DB_POSTGRESQL_USERNAME="user",
        DB_POSTGRESQL_PASSWORD="pass",
    )

    assert not settings.DB_POSTGRESQL_SOCKET_MODE
    assert settings.DB_POSTGRESQL_URL() == "postgresql://user:pass@db:5433/moviepilot"
    assert settings.DB_POSTGRESQL_URL("asyncpg") == "postgresql+asyncpg://user:pass@db:5433/moviepilot"
    assert settings.DB_POSTGRESQL_TARGET == "db:5433"


def test_postgresql_socket_url_uses_host_query_param() -> None:
    """
    PostgreSQL Unix Socket 连接通过查询参数传递目录。
    """
    settings = Settings(
        DB_POSTGRESQL_HOST="/var/run/postgresql",
        DB_POSTGRESQL_PORT="",
        DB_POSTGRESQL_DATABASE="moviepilot",
        DB_POSTGRESQL_USERNAME="user",
        DB_POSTGRESQL_PASSWORD="pass",
    )

    assert settings.DB_POSTGRESQL_SOCKET_MODE
    # socket 模式下不带端口：未显式设置时 DB_POSTGRESQL_PORT 为空串
    assert settings.DB_POSTGRESQL_PORT == ""
    assert settings.DB_POSTGRESQL_URL() == "postgresql://user:pass@/moviepilot?host=%2Fvar%2Frun%2Fpostgresql"
    assert (
        settings.DB_POSTGRESQL_URL("asyncpg")
        == "postgresql+asyncpg://user:pass@/moviepilot?host=%2Fvar%2Frun%2Fpostgresql"
    )
    assert settings.DB_POSTGRESQL_TARGET == "socket /var/run/postgresql"


def test_postgresql_socket_url_can_keep_explicit_port() -> None:
    """
    PostgreSQL Socket 连接保留显式端口参数。
    """
    settings = Settings(
        DB_POSTGRESQL_HOST="/var/run/postgresql",
        DB_POSTGRESQL_PORT="5432",
        DB_POSTGRESQL_DATABASE="moviepilot",
        DB_POSTGRESQL_USERNAME="user",
        DB_POSTGRESQL_PASSWORD="",
    )

    assert settings.DB_POSTGRESQL_URL() == "postgresql://user@/moviepilot?host=%2Fvar%2Frun%2Fpostgresql&port=5432"
    assert settings.DB_POSTGRESQL_TARGET == "socket /var/run/postgresql (port 5432)"
