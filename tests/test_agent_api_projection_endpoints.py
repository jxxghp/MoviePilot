"""Focused tests for safe user-level Agent API projections."""

import asyncio
from types import SimpleNamespace

from app.api.endpoints import site as site_endpoint
from app.api.endpoints import storage as storage_endpoint
from app.api.endpoints import workflow as workflow_endpoint
from app.schemas.file import FileItem
from app.schemas.token import TokenPayload


class _SiteQuery:
    """Return a stable mixed site list for projection tests."""

    async def list_ordered(
        self,
        *,
        is_active=None,
        name=None,
        site_ids=None,
        page=None,
        count=None,
    ):
        """Apply the endpoint's database-query contract to two fixed sites."""
        common = {
            "domain": "example.invalid",
            "url": "https://example.invalid/",
            "pri": 0,
            "downloader": "main",
            "ua": "agent-test",
            "proxy": False,
            "filter": None,
            "render": False,
            "public": False,
            "note": None,
            "limit_interval": None,
            "limit_count": None,
            "limit_seconds": None,
            "timeout": 30,
            "rss": "https://example.invalid/rss",
            "cookie": "secret-cookie",
            "apikey": "secret-key",
            "token": "secret-token",
        }
        sites = [
            SimpleNamespace(id=1, name="Active Site", is_active=True, **common),
            SimpleNamespace(id=2, name="Inactive Site", is_active=False, **common),
        ]
        if is_active is not None:
            sites = [site for site in sites if site.is_active is is_active]
        if name:
            sites = [site for site in sites if name.lower() in site.name.lower()]
        if site_ids is not None:
            sites = [site for site in sites if site.id in site_ids]
        if page is not None and count is not None:
            offset = (page - 1) * count
            sites = sites[offset:offset + count]
        return sites

    async def get_by_domain(self, domain):
        """按域名返回第一个固定站点。"""
        sites = await self.list_ordered()
        return next((site for site in sites if site.domain == domain), None)


class _WorkflowQuery:
    """Return workflows with private action context that the Agent projection must omit."""

    async def list(
        self,
        *,
        state=None,
        name=None,
        trigger_type=None,
        page=None,
        count=None,
    ):
        """Apply the endpoint's database-query contract to two fixed workflows."""
        workflows = [
            SimpleNamespace(
                id=1,
                name="Manual Workflow",
                description="visible",
                trigger_type="manual",
                state="R",
                run_count=2,
                timer=None,
                event_type=None,
                add_time="2026-08-31",
                last_time="2026-08-31",
                current_action=1,
                actions=[{"private": "context"}],
                result={"private": "result"},
            ),
            SimpleNamespace(
                id=2,
                name="Timer Workflow",
                description=None,
                trigger_type=None,
                state="W",
                run_count=0,
                timer="0 0 * * *",
                event_type=None,
                add_time="2026-08-31",
                last_time=None,
                current_action=None,
                actions=[],
                result=None,
            ),
        ]
        if state:
            workflows = [workflow for workflow in workflows if workflow.state == state]
        if name:
            workflows = [
                workflow
                for workflow in workflows
                if name.lower() in workflow.name.lower()
            ]
        if trigger_type:
            workflows = [
                workflow
                for workflow in workflows
                if workflow.trigger_type == trigger_type
            ]
        if page is not None and count is not None:
            offset = (page - 1) * count
            workflows = workflows[offset:offset + count]
        return workflows


def test_site_agent_projection_filters_and_hides_secrets_for_normal_users() -> None:
    """Normal users may list sites but must not receive authentication material."""
    result = asyncio.run(
        site_endpoint.read_agent_sites(
            status="active",
            name="active",
            query=_SiteQuery(),
            current_user=SimpleNamespace(is_superuser=False),
        )
    )

    assert [item["name"] for item in result] == ["Active Site"]
    assert all(key not in result[0] for key in ("rss", "cookie", "apikey", "token"))


def test_site_agent_projection_returns_auth_fields_only_to_superusers() -> None:
    """A verified superuser keeps the old administrator site-query fidelity."""
    result = asyncio.run(
        site_endpoint.read_agent_sites(
            status="inactive",
            query=_SiteQuery(),
            current_user=SimpleNamespace(is_superuser=True),
        )
    )

    assert result[0]["name"] == "Inactive Site"
    assert result[0]["cookie"] == "secret-cookie"
    assert result[0]["apikey"] == "secret-key"


def _token(*, super_user: bool) -> TokenPayload:
    return TokenPayload(sub=1, username="tester", super_user=super_user, purpose="authentication")


def test_site_rss_list_hides_secrets_for_normal_users(monkeypatch) -> None:
    """订阅站点列表对普通用户只返回选择站点所需的字段，不返回认证凭据。"""
    monkeypatch.setattr(
        site_endpoint,
        "get_configured_system_config",
        lambda: SimpleNamespace(get=lambda _key: []),
    )

    result = asyncio.run(
        site_endpoint.read_rss_sites(query=_SiteQuery(), token_payload=_token(super_user=False))
    )

    assert [item["name"] for item in result] == ["Active Site", "Inactive Site"]
    assert all(
        key not in item
        for item in result
        for key in ("rss", "cookie", "apikey", "token")
    )


def test_site_rss_list_keeps_secrets_for_superusers(monkeypatch) -> None:
    """超级管理员读取订阅站点列表时保持原有完整字段。"""
    monkeypatch.setattr(
        site_endpoint,
        "get_configured_system_config",
        lambda: SimpleNamespace(get=lambda _key: ["1"]),
    )

    result = asyncio.run(
        site_endpoint.read_rss_sites(query=_SiteQuery(), token_payload=_token(super_user=True))
    )

    assert [site.name for site in result] == ["Active Site"]
    assert result[0].cookie == "secret-cookie"


def test_site_by_domain_hides_secrets_for_normal_users() -> None:
    """按域名查询站点时，普通用户拿不到认证凭据，超级管理员保持完整字段。"""
    normal = asyncio.run(
        site_endpoint.read_site_by_domain(
            site_url="https://example.invalid/",
            query=_SiteQuery(),
            token_payload=_token(super_user=False),
        )
    )
    admin = asyncio.run(
        site_endpoint.read_site_by_domain(
            site_url="https://example.invalid/",
            query=_SiteQuery(),
            token_payload=_token(super_user=True),
        )
    )

    assert normal["domain"] == "example.invalid"
    assert all(key not in normal for key in ("rss", "cookie", "apikey", "token"))
    assert admin.apikey == "secret-key"


def test_workflow_agent_projection_filters_without_returning_action_context() -> None:
    """User-level workflow discovery must expose state without private execution payloads."""
    result = asyncio.run(
        workflow_endpoint.list_agent_workflows(
            state="R",
            name="manual",
            trigger_type="manual",
            query=_WorkflowQuery(),
            _=object(),
        )
    )

    assert result == [
        {
            "id": 1,
            "name": "Manual Workflow",
            "description": "visible",
            "trigger_type": "manual",
            "state": "R",
            "run_count": 2,
            "timer": None,
            "event_type": None,
            "add_time": "2026-08-31",
            "last_time": "2026-08-31",
            "current_action": 1,
        }
    ]


def test_storage_agent_list_reuses_bounded_filter_and_sort(monkeypatch) -> None:
    """User-level storage reads must preserve keyword filtering and stable sorting."""
    class _StorageChain:
        """Return a fixed directory listing without touching a real storage provider."""

        def list_files(self, _fileitem):
            """Return two entries in reverse natural-name order."""
            return [
                FileItem(path="/b", name="Episode 10", modify_time=1),
                FileItem(path="/a", name="Episode 2", modify_time=2),
            ]

    monkeypatch.setattr(storage_endpoint, "StorageChain", _StorageChain)

    result = storage_endpoint.list_agent_files(
        fileitem=FileItem(path="/"),
        sort="name",
        keyword="Episode*",
        _=object(),
    )

    assert [item.name for item in result] == ["Episode 2", "Episode 10"]
