"""通知器协议。"""
from typing import Protocol

from ..models import Digest, User


class Notifier(Protocol):
    channel: str

    async def send(self, user: User, digest: Digest) -> dict:
        """发送简报,返回 {"status": "sent"|"failed", "error"?, "payload"?}。"""
        ...
