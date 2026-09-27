"""开发用控制台通知器:零配置,直接打印。"""
from ..models import Digest, User


class ConsoleNotifier:
    channel = "console"

    def __init__(self, max_chars: int = 2000):
        self.max_chars = max_chars

    async def send(self, user: User, digest: Digest) -> dict:
        content = digest.content_md or ""
        if len(content) > self.max_chars:
            content = content[: self.max_chars] + "\n...(截断)"
        print(
            "\n" + "=" * 60
            + f"\n[通知] 用户 {user.name or user.email} 的简报:\n{content}\n"
            + "=" * 60,
            flush=True,  # 服务日志重定向到文件时 stdout 是块缓冲,必须 flush
        )
        return {"status": "sent", "payload": {}}
