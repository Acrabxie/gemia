"""Transactional email delivery over SMTP (login verification codes).

Configuration is resolved with this precedence (first non-empty wins):

  1. Environment variables ``GEMIA_SMTP_*``
  2. The ``smtp`` block of ``~/.gemia/config.json``

Nothing is hard-coded here — credentials live only in local config (chmod
600) or the environment, never in source control or shared memory.

The authenticated account is always used as the SMTP envelope sender
(``MAIL FROM``). By default the visible ``From:`` is also aligned to that
authenticated address, because Outlook/Gmail tend to junk one-time-code mail
when the visible From domain and SMTP identity do not authenticate as the same
sender. A branded From alias can still be used by setting
``allow_from_alias=true`` after the alias/domain is verified with the relay.
"""
from __future__ import annotations

import json
import os
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Any

from gemia.errors import GemiaError

CONFIG_PATH = Path.home() / ".gemia" / "config.json"
DEFAULT_PORT = 587
DEFAULT_TIMEOUT = 20
DEFAULT_FROM_NAME = "Lumeri"
DEFAULT_APP_NAME = "Lumeri"
DEFAULT_ALLOW_FROM_ALIAS = False

# Brand palette (matches the Lumeri mark) for the HTML email.
_BRAND_PRIMARY = "#5FC6DE"
_BRAND_DEEP = "#2B8FA8"
_INK = "#1c2630"
_MUTED = "#6b7a86"


class EmailDeliveryError(GemiaError):
    """Raised when an outbound transactional email cannot be sent."""

    code = "E_EMAIL"


def _clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _read_config_block() -> dict[str, Any]:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    block = data.get("smtp")
    return block if isinstance(block, dict) else {}


def _cfg(env_name: str, key: str, block: dict[str, Any], default: str = "") -> str:
    env_val = _clean(os.environ.get(env_name))
    if env_val:
        return env_val
    return _clean(block.get(key)) or default


def _cfg_bool(env_name: str, key: str, block: dict[str, Any], default: bool = False) -> bool:
    env_val = _clean(os.environ.get(env_name))
    if env_val:
        return env_val.strip().lower() in {"1", "true", "yes", "on"}
    if key in block:
        return str(block.get(key)).strip().lower() in {"1", "true", "yes", "on"}
    return default


def _addr_domain(addr: str) -> str:
    value = _clean(addr).lower()
    return value.rsplit("@", 1)[1] if "@" in value else ""


def smtp_config() -> dict[str, Any]:
    """Return the resolved SMTP settings (env overrides local config)."""
    block = _read_config_block()
    username = _cfg("GEMIA_SMTP_USERNAME", "username", block)
    password = _clean(os.environ.get("GEMIA_SMTP_PASSWORD")) or _clean(block.get("password"))
    from_addr = _cfg("GEMIA_SMTP_FROM", "from_addr", block) or username
    allow_from_alias = _cfg_bool(
        "GEMIA_SMTP_ALLOW_FROM_ALIAS",
        "allow_from_alias",
        block,
        DEFAULT_ALLOW_FROM_ALIAS,
    )
    if username and from_addr and not allow_from_alias and _addr_domain(username) != _addr_domain(from_addr):
        from_addr = username
    port_raw = _cfg("GEMIA_SMTP_PORT", "port", block) or str(DEFAULT_PORT)
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    starttls_raw = _cfg("GEMIA_SMTP_STARTTLS", "starttls", block) or "1"
    starttls = str(starttls_raw).strip().lower() not in {"0", "false", "no", "off"}
    return {
        "host": _cfg("GEMIA_SMTP_HOST", "host", block),
        "port": port,
        "username": username,
        "password": password,
        "from_addr": from_addr,
        "configured_from_addr": _cfg("GEMIA_SMTP_FROM", "from_addr", block) or username,
        "allow_from_alias": allow_from_alias,
        "from_name": _cfg("GEMIA_SMTP_FROM_NAME", "from_name", block) or DEFAULT_FROM_NAME,
        "reply_to": _cfg("GEMIA_SMTP_REPLY_TO", "reply_to", block) or from_addr,
        "starttls": starttls,
        "timeout": DEFAULT_TIMEOUT,
    }


CF_API_BASE = "https://api.cloudflare.com/client/v4"


def cf_config() -> dict[str, Any]:
    """Resolve Cloudflare Email Sending settings (env overrides local config)."""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    block = data.get("cloudflare_email") if isinstance(data, dict) else {}
    block = block if isinstance(block, dict) else {}
    from_addr = _cfg("GEMIA_EMAIL_FROM", "from_addr", block) or "login@lumeri.io"
    return {
        "account_id": _clean(os.environ.get("GEMIA_CF_ACCOUNT_ID")) or _clean(block.get("account_id")),
        "api_token": _clean(os.environ.get("GEMIA_CF_API_TOKEN")) or _clean(block.get("api_token")),
        "from_addr": from_addr,
        "from_name": _cfg("GEMIA_EMAIL_FROM_NAME", "from_name", block) or DEFAULT_FROM_NAME,
        "reply_to": _cfg("GEMIA_EMAIL_REPLY_TO", "reply_to", block) or from_addr,
        "timeout": DEFAULT_TIMEOUT,
    }


def cf_is_configured() -> bool:
    """True when a Cloudflare account id, token and from address are all present."""
    cfg = cf_config()
    return bool(cfg["account_id"] and cfg["api_token"] and cfg["from_addr"])


def _send_via_cloudflare(
    recipient: str, subject: str, text_body: str, html_body: str | None, cfg: dict[str, Any]
) -> None:
    payload: dict[str, Any] = {
        "to": recipient,
        "from": {"address": cfg["from_addr"], "name": cfg["from_name"]},  # REST uses `address`, not `email`
        "subject": subject,
        "text": text_body,
    }
    if html_body:
        payload["html"] = html_body
    if cfg.get("reply_to"):
        payload["reply_to"] = cfg["reply_to"]  # REST is snake_case

    url = f"{CF_API_BASE}/accounts/{cfg['account_id']}/email/sending/send"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {cfg['api_token']}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace") if hasattr(exc, "read") else str(exc)
        raise EmailDeliveryError(f"Cloudflare 发信失败（HTTP {exc.code}）", detail=detail) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise EmailDeliveryError(f"Cloudflare 发信失败：{exc}", detail=str(exc)) from exc

    if not body.get("success", False):
        errs = body.get("errors") or []
        msg = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in errs) or "unknown error"
        raise EmailDeliveryError(f"Cloudflare 拒绝发信：{msg}", detail=json.dumps(body, ensure_ascii=False))
    bounced = (body.get("result") or {}).get("permanent_bounces") or []
    if bounced:
        raise EmailDeliveryError(
            f"收件地址被退回：{', '.join(bounced)}",
            detail=json.dumps(body.get("result"), ensure_ascii=False),
        )


def is_configured() -> bool:
    """True when either Cloudflare or SMTP delivery is fully configured."""
    if cf_is_configured():
        return True
    cfg = smtp_config()
    return bool(cfg["host"] and cfg["username"] and cfg["password"] and cfg["from_addr"])


def send_email(to_addr: str, subject: str, text_body: str, *, html_body: str | None = None) -> None:
    """Send a single email over the configured SMTP relay (STARTTLS)."""
    recipient = _clean(to_addr)
    if not recipient:
        raise EmailDeliveryError("收件人邮箱为空")

    # Prefer Cloudflare Email Sending (HTTPS, port 443) when configured — it
    # sidesteps the blocked outbound SMTP entirely. Fall back to SMTP otherwise.
    if cf_is_configured():
        _send_via_cloudflare(recipient, subject, text_body, html_body, cf_config())
        return

    cfg = smtp_config()
    missing = [name for name in ("host", "username", "password", "from_addr") if not cfg.get(name)]
    if missing:
        raise EmailDeliveryError(
            "邮件发送未配置：缺少 " + ", ".join(missing),
            detail="Set the smtp block in ~/.gemia/config.json or GEMIA_SMTP_* env vars.",
        )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg["from_name"], cfg["from_addr"]))
    if cfg.get("allow_from_alias") and cfg["username"] and cfg["username"] != cfg["from_addr"]:
        msg["Sender"] = cfg["username"]
    msg["To"] = recipient
    if cfg["reply_to"]:
        msg["Reply-To"] = cfg["reply_to"]
    msg["Date"] = formatdate(localtime=True)
    msg["Auto-Submitted"] = "auto-generated"
    msg["X-Auto-Response-Suppress"] = "All"
    try:
        msg["Message-ID"] = make_msgid(domain=(cfg["from_addr"].split("@")[-1] or None))
    except Exception:
        pass
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    if cfg["host"] in ("127.0.0.1", "localhost"):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=cfg["timeout"]) as smtp:
            smtp.ehlo()
            if cfg["starttls"]:
                smtp.starttls(context=context)
                smtp.ehlo()
            smtp.login(cfg["username"], cfg["password"])
            # Envelope sender = authenticated account so the relay accepts it;
            # the visible From: header may still be the branded domain address.
            smtp.send_message(msg, from_addr=cfg["username"], to_addrs=[recipient])
    except EmailDeliveryError:
        raise
    except smtplib.SMTPAuthenticationError as exc:
        raise EmailDeliveryError(
            "SMTP 认证失败：请检查用户名与应用专用密码",
            detail=str(exc),
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailDeliveryError(f"邮件发送失败：{exc}", detail=str(exc)) from exc


def send_login_code(to_addr: str, code: str, *, ttl_minutes: int = 10, app_name: str = DEFAULT_APP_NAME) -> None:
    """Send a one-time login verification code email."""
    subject = f"{app_name} 登录验证码"
    text_body = (
        f"您的 {app_name} 登录验证码是：{code}\n\n"
        f"验证码 {ttl_minutes} 分钟内有效，请勿向任何人泄露。\n"
        f"如果这不是您本人的操作，请忽略本邮件，您的账户依然安全。\n"
    )
    send_email(to_addr, subject, text_body, html_body=_login_code_html(code, ttl_minutes=ttl_minutes, app_name=app_name))


def _login_code_html(code: str, *, ttl_minutes: int, app_name: str) -> str:
    spaced = " ".join(list(code))
    return f"""\
<!doctype html>
<html lang="zh">
  <body style="margin:0;padding:0;background:#f4f7f9;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f7f9;padding:32px 0;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                 style="max-width:440px;background:#ffffff;border-radius:16px;overflow:hidden;
                        box-shadow:0 6px 24px rgba(28,38,48,0.08);
                        font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;">
            <tr>
              <td style="height:6px;background:linear-gradient(90deg,{_BRAND_PRIMARY},{_BRAND_DEEP});"></td>
            </tr>
            <tr>
              <td style="padding:36px 40px 8px;">
                <div style="font-size:18px;font-weight:700;color:{_INK};letter-spacing:.2px;">{app_name}</div>
                <div style="margin-top:24px;font-size:15px;color:{_INK};">您正在登录 {app_name}，请使用以下验证码完成验证：</div>
              </td>
            </tr>
            <tr>
              <td style="padding:8px 40px 4px;">
                <div style="margin:18px 0;padding:18px 0;text-align:center;background:#f0fafd;
                            border:1px solid #d7eef5;border-radius:12px;
                            font-size:34px;font-weight:700;letter-spacing:10px;color:{_BRAND_DEEP};">{spaced}</div>
              </td>
            </tr>
            <tr>
              <td style="padding:4px 40px 36px;">
                <div style="font-size:13px;color:{_MUTED};line-height:1.7;">
                  验证码 {ttl_minutes} 分钟内有效，请勿向任何人泄露。<br>
                  如果这不是您本人的操作，请忽略本邮件，您的账户依然安全。
                </div>
              </td>
            </tr>
          </table>
          <div style="margin-top:16px;font-size:12px;color:#9aa7b1;">© {app_name}</div>
        </td>
      </tr>
    </table>
  </body>
</html>
"""


__all__ = [
    "EmailDeliveryError",
    "cf_config",
    "cf_is_configured",
    "is_configured",
    "send_email",
    "send_login_code",
    "smtp_config",
]
