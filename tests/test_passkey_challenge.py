from concurrent.futures import ThreadPoolExecutor

from app.core.cache import TTLCache
from app.helper.passkey import PasskeyChallengeStore


def setup_function():
    PasskeyChallengeStore._cache.clear()


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
