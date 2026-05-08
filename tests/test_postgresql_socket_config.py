import sys
import unittest
from enum import Enum
from types import ModuleType


def _stub_module(name: str, **attrs):
    module = sys.modules.get(name)
    if module is None:
        module = ModuleType(name)
        sys.modules[name] = module
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


class _DummyLogger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


_stub_module(
    "app.log",
    logger=_DummyLogger(),
    log_settings=_DummyLogger(),
    LogConfigModel=type("LogConfigModel", (), {}),
)
_stub_module("psutil")
_schemas_module = _stub_module(
    "app.schemas", MediaType=Enum("MediaType", {"Movie": "Movie", "TV": "TV"})
)
_schemas_module.__getattr__ = lambda name: type(name, (), {})
_stub_module("version", APP_VERSION="test")


from app.core.config import Settings


class PostgreSQLSocketConfigTests(unittest.TestCase):
    def test_postgresql_tcp_url_keeps_host_and_port(self):
        settings = Settings(
            DB_POSTGRESQL_HOST="db",
            DB_POSTGRESQL_PORT="5433",
            DB_POSTGRESQL_DATABASE="moviepilot",
            DB_POSTGRESQL_USERNAME="user",
            DB_POSTGRESQL_PASSWORD="pass",
        )

        self.assertFalse(settings.DB_POSTGRESQL_SOCKET_MODE)
        self.assertEqual(
            settings.DB_POSTGRESQL_URL(),
            "postgresql://user:pass@db:5433/moviepilot",
        )
        self.assertEqual(
            settings.DB_POSTGRESQL_URL("asyncpg"),
            "postgresql+asyncpg://user:pass@db:5433/moviepilot",
        )
        self.assertEqual(settings.DB_POSTGRESQL_TARGET, "db:5433")

    def test_postgresql_socket_url_uses_host_query_param(self):
        settings = Settings(
            DB_POSTGRESQL_HOST="/var/run/postgresql",
            DB_POSTGRESQL_PORT="",
            DB_POSTGRESQL_DATABASE="moviepilot",
            DB_POSTGRESQL_USERNAME="user",
            DB_POSTGRESQL_PASSWORD="pass",
        )

        self.assertTrue(settings.DB_POSTGRESQL_SOCKET_MODE)
        self.assertIsNone(settings.DB_POSTGRESQL_PORT_VALUE)
        self.assertEqual(
            settings.DB_POSTGRESQL_URL(),
            "postgresql://user:pass@/moviepilot?host=%2Fvar%2Frun%2Fpostgresql",
        )
        self.assertEqual(
            settings.DB_POSTGRESQL_URL("asyncpg"),
            "postgresql+asyncpg://user:pass@/moviepilot?host=%2Fvar%2Frun%2Fpostgresql",
        )
        self.assertEqual(settings.DB_POSTGRESQL_TARGET, "socket /var/run/postgresql")

    def test_postgresql_socket_url_can_keep_explicit_port(self):
        settings = Settings(
            DB_POSTGRESQL_HOST="/var/run/postgresql",
            DB_POSTGRESQL_PORT="5432",
            DB_POSTGRESQL_DATABASE="moviepilot",
            DB_POSTGRESQL_USERNAME="user",
            DB_POSTGRESQL_PASSWORD="",
        )

        self.assertEqual(
            settings.DB_POSTGRESQL_URL(),
            "postgresql://user@/moviepilot?host=%2Fvar%2Frun%2Fpostgresql&port=5432",
        )
        self.assertEqual(
            settings.DB_POSTGRESQL_TARGET,
            "socket /var/run/postgresql (port 5432)",
        )


if __name__ == "__main__":
    unittest.main()
