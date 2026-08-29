"""验证 composition owner 对称撤销其发布的 lifespan provider。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app.adapters.external import server as server_provider
from app.adapters.web.security import access as access_provider
from app.application import agenttask as task_provider
from app.application import image as image_provider
from app.application import network as network_provider
from app.application import outbox as outbox_provider
from app.application.chain import context as chain_provider
from app.application.messaging import chat as chat_provider
from app.application.messaging import ingress as ingress_provider
from app.application.security import auth as auth_provider
from app.application.security import passkey as passkey_provider
from app.application.security import user as user_provider
from app.startup.composition import agent as agent_composition
from app.startup.composition import chain as chain_composition
from app.startup.composition import network as network_composition
from app.startup.composition import outbox as outbox_composition
from app.startup.composition import security as security_composition
from app.startup.composition import server as server_composition


def test_agent_owner_reset_is_symmetric_and_idempotent(monkeypatch) -> None:
    """Agent owner 撤销数据上下文、任务执行与两类会话服务。"""
    monkeypatch.setattr(chat_provider, "_configured_agent_chat_service", None)
    monkeypatch.setattr(chat_provider, "_configured_agent_chat_persistence", None)
    monkeypatch.setattr(task_provider, "_service", None)
    data_state: dict[str, object] = {}
    composition = SimpleNamespace(
        data=SimpleNamespace(chat=object(), chat_persistence=object()),
        execution=object(),
    )

    agent_composition.publish_agent_services(
        composition,
        data_context_registrar=lambda data: data_state.update(current=data),
    )
    assert chat_provider.get_configured_agent_chat_service() is composition.data.chat
    assert (
        chat_provider.get_configured_agent_chat_persistence()
        is composition.data.chat_persistence
    )
    assert task_provider.get_agent_task_execution_service() is composition.execution
    assert data_state["current"] is composition.data

    def reset_data() -> None:
        """撤销测试中的 Agent 数据上下文。"""
        data_state.pop("current", None)

    agent_composition.reset_agent_services(data_context_resetter=reset_data)
    agent_composition.reset_agent_services(data_context_resetter=reset_data)

    assert data_state == {}
    for getter in (
        chat_provider.get_configured_agent_chat_service,
        chat_provider.get_configured_agent_chat_persistence,
        task_provider.get_agent_task_execution_service,
    ):
        with pytest.raises(RuntimeError):
            getter()


def test_security_owner_reset_is_symmetric_and_idempotent(monkeypatch) -> None:
    """Security owner 撤销身份查询、认证、PassKey 与 Web 访问 provider。"""
    monkeypatch.setattr(user_provider, "_configured_user_id_lookup", None)
    monkeypatch.setattr(user_provider, "_configured_user_name_lookup", None)
    monkeypatch.setattr(user_provider, "_configured_user_channel_lookup", None)
    monkeypatch.setattr(auth_provider, "_configured_auth_service", None)
    monkeypatch.setattr(passkey_provider.PasskeyChallengeStore, "_cache", None)
    monkeypatch.setattr(passkey_provider, "_configured_passkey_service", None)
    monkeypatch.setattr(access_provider, "_superuser_token_payload_provider", None)
    access_provider._create_superuser_token_payload.cache_clear()

    user_provider.configure_user_lookups(
        by_id=lambda _user_id: object(),
        by_name=lambda _username: object(),
        by_channel=lambda **_bindings: "user",
    )
    auth_provider.configure_auth_service(object())  # type: ignore[arg-type]
    passkey_provider.configure_passkey_challenge_cache(object())  # type: ignore[arg-type]
    passkey_provider.configure_passkey_service(object())  # type: ignore[arg-type]
    access_provider.set_superuser_token_payload_provider(lambda: object())

    security_composition.reset_security_access()
    security_composition.reset_security_access()
    security_composition.reset_security_services()
    security_composition.reset_security_services()

    for getter in (
        user_provider.get_configured_user_id_lookup,
        user_provider.get_configured_user_name_lookup,
        user_provider.get_configured_user_channel_lookup,
        auth_provider.get_configured_auth_service,
        passkey_provider.get_configured_passkey_service,
        passkey_provider.PasskeyChallengeStore._get_cache,
    ):
        with pytest.raises(RuntimeError):
            getter()
    with pytest.raises(HTTPException) as error:
        access_provider._create_superuser_token_payload()
    assert error.value.status_code == 503


def test_network_owner_reset_is_symmetric_and_idempotent(monkeypatch) -> None:
    """Network owner 撤销探测、图片和消息回环端口。"""
    monkeypatch.setattr(network_provider, "_configured_network_test_service", None)
    monkeypatch.setattr(image_provider, "_image_transport", None)
    monkeypatch.setattr(image_provider, "_internal_address", None)
    monkeypatch.setattr(ingress_provider, "_message_ingress_port", None)

    network_provider.configure_network_test_service(object())  # type: ignore[arg-type]
    image_provider.configure_image_ports(
        transport=object(),  # type: ignore[arg-type]
        internal_address=object(),  # type: ignore[arg-type]
    )
    ingress_provider.configure_message_ingress_port(object())  # type: ignore[arg-type]

    network_composition.reset_application_network_ports()
    network_composition.reset_application_network_ports()

    for getter in (
        network_provider.get_configured_network_test_service,
        image_provider._image_ports_snapshot,
        ingress_provider._message_ingress_snapshot,
    ):
        with pytest.raises(RuntimeError):
            getter()


def test_outbox_owner_reset_is_symmetric_and_idempotent(monkeypatch) -> None:
    """Outbox owner 撤销 dispatcher 工厂并恢复显式未配置错误。"""
    monkeypatch.setattr(outbox_provider, "_configured_dispatcher", None)
    outbox_provider.configure_outbox_dispatcher(Mock())

    outbox_composition.reset_outbox_services()
    outbox_composition.reset_outbox_services()

    with pytest.raises(RuntimeError, match="Outbox dispatcher 尚未配置"):
        outbox_provider.dispatch_pending_outbox()


def test_server_owner_reset_is_symmetric_and_idempotent(monkeypatch) -> None:
    """Server owner 同时撤销上报与分享服务。"""
    monkeypatch.setattr(server_provider, "_server_report_service", None)
    monkeypatch.setattr(server_provider, "_server_sharing_service", None)
    server_provider.configure_server_application_services(
        report_service=object(),
        sharing_service=object(),
    )

    server_composition.reset_server_services()
    server_composition.reset_server_services()

    with pytest.raises(RuntimeError):
        server_provider.MoviePilotServerHelper._report_service()
    with pytest.raises(RuntimeError):
        server_provider.MoviePilotServerHelper._sharing_service()


def test_chain_owner_reset_is_symmetric_and_idempotent(monkeypatch) -> None:
    """Chain owner 撤销无参上下文，并把壁纸来源恢复为空结果。"""
    monkeypatch.setattr(
        chain_provider,
        "_context_provider",
        chain_provider._unconfigured_chain_runtime_context,
    )
    chain_provider.configure_chain_runtime_context_provider(lambda: object())
    image_provider.configure_wallpaper_providers(
        tmdb_wallpaper=lambda: "tmdb",
        tmdb_wallpapers=lambda _count: ["tmdb"],
        mediaserver_wallpaper=lambda: "media",
        mediaserver_wallpapers=lambda _count: ["media"],
    )

    chain_composition.reset_chain_services()
    chain_composition.reset_chain_services()

    with pytest.raises(RuntimeError, match="Chain 运行上下文尚未由启动组合根配置"):
        chain_provider.get_chain_runtime_context()
    wallpaper = image_provider.WallpaperHelper()
    assert wallpaper.get_tmdb_wallpaper() is None
    assert wallpaper.get_tmdb_wallpapers() == []
    assert wallpaper.get_mediaserver_wallpaper() is None
    assert wallpaper.get_mediaserver_wallpapers() == []


def test_composition_owner_resets_run_in_reverse_publication_order(monkeypatch) -> None:
    """多 provider owner 必须按发布逆序撤销，避免依赖先于消费者消失。"""
    calls: list[str] = []

    for module, names in (
        (
            agent_composition,
            (
                "reset_agent_task_execution",
                "reset_agent_chat_persistence",
                "reset_agent_chat_service",
            ),
        ),
        (
            security_composition,
            (
                "reset_passkey_service",
                "reset_passkey_challenge_cache",
                "reset_auth_service",
                "reset_user_lookups",
            ),
        ),
        (
            network_composition,
            (
                "reset_message_ingress_port",
                "reset_image_ports",
                "reset_network_test_service",
            ),
        ),
        (
            chain_composition,
            (
                "configure_chain_runtime_context_provider",
                "reset_wallpaper_providers",
            ),
        ),
    ):
        for name in names:
            monkeypatch.setattr(
                module,
                name,
                lambda *args, _name=name, **kwargs: calls.append(_name),
            )

    agent_composition.reset_agent_services(
        data_context_resetter=lambda: calls.append("reset_agent_data_context"),
    )
    security_composition.reset_security_services()
    network_composition.reset_application_network_ports()
    chain_composition.reset_chain_services()

    assert calls == [
        "reset_agent_data_context",
        "reset_agent_task_execution",
        "reset_agent_chat_persistence",
        "reset_agent_chat_service",
        "reset_passkey_service",
        "reset_passkey_challenge_cache",
        "reset_auth_service",
        "reset_user_lookups",
        "reset_message_ingress_port",
        "reset_image_ports",
        "reset_network_test_service",
        "configure_chain_runtime_context_provider",
        "reset_wallpaper_providers",
    ]
