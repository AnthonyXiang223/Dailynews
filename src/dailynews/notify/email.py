"""邮件推送:SMTP + 极简 markdown→HTML 渲染(零外部依赖)。

QQ 邮箱示例:smtp_host=smtp.qq.com,端口 465(SSL),密码填「授权码」
(邮箱设置→账户→开启 SMTP 服务后生成),不是邮箱登录密码。

注意:smtplib 是同步阻塞调用,必须放进 asyncio.to_thread,
否则发信期间整个事件循环被冻结(曾导致列表接口排队 21 秒)。
"""
import asyncio
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from ..config import get_settings
from ..models import Digest, User


def _md_to_html(md: str) -> str:
    """简报用到的 markdown 子集 → HTML(标题/加粗/链接/列表/引用/分隔线)。"""
    import html
    import re

    lines = md.split("\n")
    out: list[str] = []
    list_open = False

    def inline(s: str) -> str:
        s = html.escape(s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2" style="color:#B03A2E">\1</a>', s)
        return s

    for raw in lines:
        t = raw.strip()
        if not t:
            if list_open:
                out.append("</ul>")
                list_open = False
            continue
        if re.fullmatch(r"-{3,}", t):
            out.append("<hr>")
        elif t.startswith("### "):
            out.append(f"<h3>{inline(t[4:])}</h3>")
        elif t.startswith("## "):
            out.append(f"<h2>{inline(t[3:])}</h2>")
        elif t.startswith("# "):
            out.append(f"<h1>{inline(t[2:])}</h1>")
        elif t.startswith("> "):
            out.append(f'<blockquote style="border-left:3px solid #ddd;margin:8px 0;padding-left:12px;color:#666">{inline(t[2:])}</blockquote>')
        elif re.match(r"^[-*] ", t):
            if not list_open:
                out.append("<ul>")
                list_open = True
            out.append(f"<li>{inline(re.sub(r'^[-*] ', '', t))}</li>")
        else:
            if list_open:
                out.append("</ul>")
                list_open = False
            out.append(f"<p>{inline(t)}</p>")
    if list_open:
        out.append("</ul>")
    body = "\n".join(out)
    return f"""<div style="font-family:'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
    max-width:680px;margin:0 auto;font-size:14px;line-height:1.8;color:#211F1A">
{body}
</div>"""


def _send_smtp(host: str, port: int, user: str, password: str, from_addr: str, to_email: str, msg) -> None:
    """同步 SMTP 发送(在 to_thread 中执行)。"""
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=context, timeout=15) as server:
        server.login(user, password)
        server.sendmail(from_addr, [to_email], msg.as_string())


class EmailNotifier:
    channel = "email"

    def __init__(self, to_email: str):
        self.to_email = to_email

    async def send(self, user: User, digest: Digest) -> dict:
        settings = get_settings()
        from_addr = settings.smtp_from or settings.smtp_user
        html_body = _md_to_html(digest.content_md or "")

        msg = MIMEMultipart("alternative")
        msg["Subject"] = digest.title or "每日 AI 新闻简报"
        msg["From"] = from_addr
        msg["To"] = self.to_email
        msg.attach(MIMEText(digest.content_md or "", "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        try:
            # 阻塞的 SMTP 放进线程,不冻结事件循环
            await asyncio.to_thread(
                _send_smtp,
                settings.smtp_host, settings.smtp_port, settings.smtp_user,
                settings.smtp_password, from_addr, self.to_email, msg,
            )
            return {"status": "sent", "payload": {"to": self.to_email}}
        except Exception as exc:  # noqa: BLE001 记录为投递失败
            return {"status": "failed", "error": repr(exc), "payload": {"to": self.to_email}}
