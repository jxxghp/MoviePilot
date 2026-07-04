from app.agent.prompt import PromptManager
from app.core.config import settings


def test_moviepilot_info_does_not_expose_api_token_or_database_password(monkeypatch) -> None:
    """系统提示词中的运行信息不能暴露敏感或细粒度部署信息。"""
    monkeypatch.setattr(settings, "API_TOKEN", "prompt-secret-token")
    monkeypatch.setattr(settings, "DB_TYPE", "postgresql")
    monkeypatch.setattr(settings, "DB_POSTGRESQL_HOST", "db.example.local")
    monkeypatch.setattr(settings, "DB_POSTGRESQL_PORT", "5432")
    monkeypatch.setattr(settings, "DB_POSTGRESQL_DATABASE", "moviepilot")
    monkeypatch.setattr(settings, "DB_POSTGRESQL_USERNAME", "moviepilot_user")
    monkeypatch.setattr(settings, "DB_POSTGRESQL_PASSWORD", "prompt-db-password")

    manager = PromptManager()
    moviepilot_info = manager._get_moviepilot_info()

    assert "prompt-secret-token" not in moviepilot_info
    assert "prompt-db-password" not in moviepilot_info
    assert "moviepilot_user:prompt-db-password" not in moviepilot_info
    assert "db.example.local" not in moviepilot_info
    assert str(settings.CONFIG_PATH) in moviepilot_info
    assert str(settings.TEMP_PATH) in moviepilot_info
    assert str(settings.LOG_PATH) not in moviepilot_info
    assert str(settings.LOG_PATH / "moviepilot.log") not in moviepilot_info
    assert str(settings.LOG_PATH / "moviepilot.stdout.log") not in moviepilot_info
    assert str(settings.LOG_PATH / "moviepilot.frontend.stdout.log") not in moviepilot_info
    assert "日志目录" not in moviepilot_info
    assert "主日志文件" not in moviepilot_info
    assert str(settings.CONFIG_PATH / "agent") not in moviepilot_info
    assert str(settings.CONFIG_PATH / "agent" / "memory") not in moviepilot_info
    assert str(settings.CONFIG_PATH / "agent" / "skills") not in moviepilot_info
    assert str(settings.CONFIG_PATH / "agent" / "jobs") not in moviepilot_info
    assert str(settings.CONFIG_PATH / "agent" / "activity") not in moviepilot_info
    assert "主机名" not in moviepilot_info
    assert "IP地址" not in moviepilot_info
    assert "API端口" not in moviepilot_info
    assert "数据库类型" not in moviepilot_info
    assert "PostgreSQL" not in moviepilot_info
    assert "query_doctor_report" in moviepilot_info
    assert "query_system_settings" in moviepilot_info


def test_moviepilot_info_lists_command_names_without_paths(monkeypatch) -> None:
    """系统提示词可注入命令名称，但不应暴露命令绝对路径。"""
    command_paths = {
        "git": "/usr/bin/git",
        "rg": "/opt/homebrew/bin/rg",
        "ffmpeg": "/usr/local/bin/ffmpeg",
    }

    monkeypatch.setattr(
        "app.agent.prompt.shutil.which",
        lambda command: command_paths.get(command),
    )

    manager = PromptManager()
    moviepilot_info = manager._get_moviepilot_info()

    assert "`git`" in moviepilot_info
    assert "`rg`" in moviepilot_info
    assert "`ffmpeg`" in moviepilot_info
    assert "/usr/bin/git" not in moviepilot_info
    assert "/opt/homebrew/bin/rg" not in moviepilot_info
    assert "/usr/local/bin/ffmpeg" not in moviepilot_info
    assert "rg --files" in moviepilot_info
