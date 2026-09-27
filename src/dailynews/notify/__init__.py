"""推送通知:配置了 SMTP 发邮箱;未配置则控制台打印(开发模式)。"""
from ..config import get_settings
from ..models import User
from .base import Notifier
from .console import ConsoleNotifier
from .email import EmailNotifier


def build_notifier(user: User) -> Notifier:
    settings = get_settings()
    if settings.smtp_host and settings.smtp_user and settings.smtp_password:
        return EmailNotifier(user.email)
    return ConsoleNotifier()


__all__ = ["Notifier", "build_notifier", "ConsoleNotifier", "EmailNotifier"]
