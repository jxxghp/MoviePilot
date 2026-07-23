import secrets
import threading
from dataclasses import dataclass
from typing import Literal, Optional

from app.core.cache import TTLCache
from app.helper.redis import RedisHelper

PASSKEY_CHALLENGE_TTL_SECONDS = 5 * 60
PasskeyChallengePurpose = Literal["authentication", "registration"]


@dataclass(frozen=True)
class PasskeyChallenge:
    """服务端保存的一次性 Passkey challenge 及其认证边界。"""

    challenge: str
    purpose: PasskeyChallengePurpose
    user_id: Optional[int]


class PasskeyChallengeStore:
    """使用当前缓存后端签发并原子消费短时 Passkey challenge。"""

    _cache = TTLCache(
        region="passkey_challenge",
        maxsize=4096,
        ttl=PASSKEY_CHALLENGE_TTL_SECONDS,
    )
    _memory_consume_lock = threading.Lock()

    @classmethod
    def issue(
        cls,
        *,
        challenge: str,
        purpose: PasskeyChallengePurpose,
        user_id: Optional[int],
    ) -> str:
        """保存 challenge 并返回不携带认证事实的随机事务 token。"""
        transaction_token = secrets.token_urlsafe(32)
        cls._cache.set(
            transaction_token,
            PasskeyChallenge(
                challenge=challenge,
                purpose=purpose,
                user_id=user_id,
            ),
        )
        return transaction_token

    @classmethod
    def consume(
        cls,
        *,
        transaction_token: str,
        purpose: PasskeyChallengePurpose,
    ) -> Optional[PasskeyChallenge]:
        """原子领取 challenge；任何完成尝试都会使事务失效。"""
        if not transaction_token:
            return None

        if cls._cache.is_redis():
            challenge = RedisHelper().pop(
                transaction_token,
                region="passkey_challenge",
            )
        else:
            with cls._memory_consume_lock:
                try:
                    challenge = cls._cache.pop(transaction_token)
                except KeyError:
                    challenge = None

        if not isinstance(challenge, PasskeyChallenge):
            return None
        if challenge.purpose != purpose:
            return None
        return challenge
