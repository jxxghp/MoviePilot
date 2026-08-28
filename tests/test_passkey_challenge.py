from concurrent.futures import ThreadPoolExecutor

import pytest

from app.application.security.passkey import (
    PASSKEY_CHALLENGE_TTL_SECONDS,
    PasskeyChallengeStore,
    configure_passkey_challenge_cache,
)
from app.runtime.cache import TTLCache


def setup_function():
    configure_passkey_challenge_cache(TTLCache(
        region="passkey_challenge",
        maxsize=4096,
        ttl=PASSKEY_CHALLENGE_TTL_SECONDS,
    ))


def test_challenge_can_only_be_consumed_once():
    token = PasskeyChallengeStore.issue(
        challenge="server-challenge",
        purpose="authentication",
        user_id=1,
    )

    challenge = PasskeyChallengeStore.consume(
        transaction_token=token,
        purpose="authentication",
    )

    assert challenge is not None
    assert challenge.challenge == "server-challenge"
    assert challenge.user_id == 1
    assert (
        PasskeyChallengeStore.consume(
            transaction_token=token,
            purpose="authentication",
        )
        is None
    )


def test_wrong_purpose_invalidates_transaction():
    token = PasskeyChallengeStore.issue(
        challenge="server-challenge",
        purpose="registration",
        user_id=1,
    )

    assert (
        PasskeyChallengeStore.consume(
            transaction_token=token,
            purpose="authentication",
        )
        is None
    )
    assert (
        PasskeyChallengeStore.consume(
            transaction_token=token,
            purpose="registration",
        )
        is None
    )


def test_expired_challenge_cannot_be_consumed(monkeypatch):
    expired_cache = TTLCache(region="expired_passkey_challenge", maxsize=1, ttl=0)
    monkeypatch.setattr(PasskeyChallengeStore, "_cache", expired_cache)
    token = PasskeyChallengeStore.issue(
        challenge="server-challenge",
        purpose="authentication",
        user_id=None,
    )

    assert (
        PasskeyChallengeStore.consume(
            transaction_token=token,
            purpose="authentication",
        )
        is None
    )


def test_concurrent_consumers_have_single_winner():
    token = PasskeyChallengeStore.issue(
        challenge="server-challenge",
        purpose="authentication",
        user_id=None,
    )

    def consume():
        return PasskeyChallengeStore.consume(
            transaction_token=token,
            purpose="authentication",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: consume(), range(8)))

    assert sum(result is not None for result in results) == 1


def test_challenge_store_requires_explicit_cache(monkeypatch):
    """未完成启动装配时不得签发一个实际未保存的事务 token。"""
    monkeypatch.setattr(PasskeyChallengeStore, "_cache", None)

    with pytest.raises(RuntimeError, match="缓存尚未配置"):
        PasskeyChallengeStore.issue(
            challenge="server-challenge",
            purpose="authentication",
            user_id=None,
        )


@pytest.mark.parametrize("operation", ["store", "consume"])
def test_challenge_cache_failure_is_not_treated_as_a_cache_miss(
    monkeypatch,
    operation,
):
    """安全缓存故障必须向认证入口传播，不能伪装成正常 miss。"""
    class FailingCache:
        """模拟严格缓存写入或领取故障。"""

        def store(self, _key, _value):
            """按用例模拟写入结果。"""
            if operation == "store":
                raise RuntimeError("cache unavailable")

        def consume(self, _key):
            """按用例模拟领取结果。"""
            if operation == "consume":
                raise RuntimeError("cache unavailable")
            return None

    monkeypatch.setattr(PasskeyChallengeStore, "_cache", FailingCache())

    with pytest.raises(RuntimeError, match="cache unavailable"):
        if operation == "store":
            PasskeyChallengeStore.issue(
                challenge="server-challenge",
                purpose="authentication",
                user_id=None,
            )
        else:
            PasskeyChallengeStore.consume(
                transaction_token="transaction-token",
                purpose="authentication",
            )
