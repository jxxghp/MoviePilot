"""数据库备份所需 Docker 运行时工具合同。"""

import re
from pathlib import Path


def test_runtime_image_installs_postgresql_18_client_from_pgdg() -> None:
    """Trixie 镜像必须从签名的 PGDG 源安装固定主版本客户端。"""
    dockerfile = (
        Path(__file__).resolve().parents[1] / "docker" / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert re.search(
        r'^ARG MOVIEPILOT_PYTHON_VERSION="3\.14\.7"$',
        dockerfile,
        re.MULTILINE,
    )
    assert "FROM python:${MOVIEPILOT_PYTHON_VERSION}-slim-trixie AS base" in dockerfile
    assert "https://www.postgresql.org/media/keys/ACCC4CF8.asc" in dockerfile
    for curl_option in (
        "--connect-timeout 10",
        "--max-time 30",
        "--retry 3",
        "--retry-all-errors",
        "--retry-max-time 90",
    ):
        assert curl_option in dockerfile
    keyring = "/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc"
    assert f"chmod 0644 {keyring}" in dockerfile
    assert f"signed-by={keyring}" in dockerfile
    assert "https://apt.postgresql.org/pub/repos/apt trixie-pgdg main" in dockerfile
    assert re.search(r"^\s+postgresql-client-18 \\$", dockerfile, re.MULTILINE)
    assert not re.search(r"^\s+postgresql-client \\$", dockerfile, re.MULTILINE)


def test_runtime_image_keeps_pgdg_setup_architecture_neutral_and_cleans_apt_cache(
) -> None:
    """PGDG 原生架构解析需同时适用于 amd64/arm64，且不得遗留 APT 索引。"""
    dockerfile = (
        Path(__file__).resolve().parents[1] / "docker" / "Dockerfile"
    ).read_text(encoding="utf-8")

    pgdg_sources = [
        line for line in dockerfile.splitlines() if "apt.postgresql.org/pub/repos/apt" in line
    ]

    assert len(pgdg_sources) == 1
    pgdg_source = pgdg_sources[0]
    assert "arch=" not in pgdg_source
    runtime_packages = dockerfile[
        dockerfile.index("FROM base AS prepare_package") :
        dockerfile.index("FROM base AS prepare_venv")
    ]
    assert "/var/lib/apt/lists/*" in runtime_packages
    assert runtime_packages.index("postgresql-client-18") < runtime_packages.index(
        "/var/lib/apt/lists/*"
    )
