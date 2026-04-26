from __future__ import annotations

import base64
import socket
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from typing import Any
from urllib.parse import unquote, urlparse

from journalcheck.http import make_session
from journalcheck.utils import clean_text, env_optional


def format_change_message(changes: list[dict[str, Any]], refreshed_at: str) -> tuple[str, str]:
    subject = f"Journal status update ({len(changes)} change{'s' if len(changes) != 1 else ''})"
    lines = [f"Refresh time: {refreshed_at}", "", "Detected changes:"]
    for change in changes:
        change_type = change.get("type")
        line = f"- [{change.get('site')}] {change.get('manuscript_number')} | {change.get('title')}"
        lines.append(line)
        if change_type == "new_submission":
            lines.append(f"  New active submission: {change.get('new_status')}")
        elif change_type == "status_changed":
            lines.append(f"  Status changed: {change.get('old_status')} -> {change.get('new_status')}")
            if change.get("new_status_date"):
                lines.append(f"  Status date: {change.get('new_status_date')}")
        elif change_type == "progress_changed":
            lines.append(f"  Progress updated: {change.get('summary')}")
        elif change_type == "no_longer_active":
            lines.append(f"  No longer in active tracking. Previous status: {change.get('old_status')}")
        detail_url = change.get("detail_url")
        if detail_url:
            lines.append(f"  Link: {detail_url}")
    return subject, "\n".join(lines)


def send_notifications(changes: list[dict[str, Any]], refreshed_at: str) -> list[str]:
    if not changes:
        return []

    subject, body = format_change_message(changes, refreshed_at)
    sent: list[str] = []

    if email_configured():
        _send_email(subject, body)
        sent.append("email")

    webhook_url = env_optional("WECOM_WEBHOOK_URL", "")
    if webhook_url:
        _send_wecom(webhook_url, body)
        sent.append("wecom")

    return sent


def email_configured() -> bool:
    required = [
        env_optional("SMTP_HOST", ""),
        env_optional("SMTP_PORT", ""),
        env_optional("SMTP_USERNAME", ""),
        env_optional("SMTP_PASSWORD", ""),
        env_optional("SMTP_TO", ""),
    ]
    return all(required)


def send_test_email() -> None:
    if not email_configured():
        raise RuntimeError("Email is not fully configured in .env")
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    subject = "Journalcheck test email"
    body = "This is a test email from journalcheck.\n\nSent at: " + timestamp
    _send_email(subject, body)


def _build_message(subject: str, body: str) -> tuple[EmailMessage, str, list[str]]:
    username = env_optional("SMTP_USERNAME", "")
    to_addrs = [item.strip() for item in env_optional("SMTP_TO", "").split(",") if item.strip()]
    from_addr = env_optional("SMTP_FROM", username)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(body)
    return msg, from_addr, to_addrs


def _proxy_bypass_matches(host: str, no_proxy: str) -> bool:
    host = host.lower().strip(".")
    for raw_item in no_proxy.split(","):
        item = raw_item.strip().lower().lstrip(".")
        if not item:
            continue
        if item == "*" or host == item or host.endswith(f".{item}"):
            return True
    return False


def _smtp_proxy_url(host: str) -> str:
    if _proxy_bypass_matches(host, env_optional("NO_PROXY", "")):
        return ""
    return env_optional("HTTPS_PROXY", "") or env_optional("HTTP_PROXY", "")


def _format_connect_host(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _read_proxy_connect_response(sock: socket.socket) -> bytes:
    response = bytearray()
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(1)
        if not chunk:
            break
        response += chunk
        if len(response) > 65536:
            raise OSError("Proxy CONNECT response is too large")
    return bytes(response)


def _open_proxy_tunnel(proxy_url: str, host: str, port: int, timeout: float | object) -> socket.socket:
    parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise OSError(f"SMTP proxy only supports HTTP proxy URLs, got: {scheme or 'missing scheme'}")
    if not parsed.hostname:
        raise OSError("SMTP proxy URL is missing a host")

    proxy_port = parsed.port or (443 if scheme == "https" else 80)
    raw_sock = socket.create_connection((parsed.hostname, proxy_port), timeout=timeout)
    try:
        sock = raw_sock
        if scheme == "https":
            sock = ssl.create_default_context().wrap_socket(raw_sock, server_hostname=parsed.hostname)

        connect_host = _format_connect_host(host)
        request_lines = [
            f"CONNECT {connect_host}:{port} HTTP/1.1",
            f"Host: {connect_host}:{port}",
            "Proxy-Connection: keep-alive",
        ]
        if parsed.username:
            username = unquote(parsed.username)
            password = unquote(parsed.password or "")
            token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            request_lines.append(f"Proxy-Authorization: Basic {token}")
        request = "\r\n".join(request_lines) + "\r\n\r\n"
        sock.sendall(request.encode("ascii"))

        response = _read_proxy_connect_response(sock)
        status_line = response.split(b"\r\n", 1)[0].decode("iso-8859-1", errors="replace")
        parts = status_line.split(None, 2)
        if len(parts) < 2 or parts[1] != "200":
            raise OSError(f"Proxy CONNECT failed: {status_line or 'empty response'}")
        return sock
    except Exception:
        raw_sock.close()
        raise


class _ProxySMTP(smtplib.SMTP):
    def __init__(self, *args: Any, proxy_url: str = "", **kwargs: Any) -> None:
        self._proxy_url = proxy_url
        super().__init__(*args, **kwargs)

    def _get_socket(self, host: str, port: int, timeout: float | object) -> socket.socket:
        if self._proxy_url:
            return _open_proxy_tunnel(self._proxy_url, host, port, timeout)
        return super()._get_socket(host, port, timeout)


class _ProxySMTP_SSL(smtplib.SMTP_SSL):
    def __init__(self, *args: Any, proxy_url: str = "", **kwargs: Any) -> None:
        self._proxy_url = proxy_url
        super().__init__(*args, **kwargs)

    def _get_socket(self, host: str, port: int, timeout: float | object) -> socket.socket:
        if not self._proxy_url:
            return super()._get_socket(host, port, timeout)
        sock = _open_proxy_tunnel(self._proxy_url, host, port, timeout)
        try:
            return self.context.wrap_socket(sock, server_hostname=self._host)
        except Exception:
            sock.close()
            raise


def _send_via_ssl(host: str, port: int, username: str, password: str, msg: EmailMessage) -> None:
    with _ProxySMTP_SSL(host, port, timeout=30, proxy_url=_smtp_proxy_url(host)) as server:
        server.login(username, password)
        server.send_message(msg)


def _send_via_starttls(host: str, port: int, username: str, password: str, msg: EmailMessage) -> None:
    with _ProxySMTP(host, port, timeout=30, proxy_url=_smtp_proxy_url(host)) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(username, password)
        server.send_message(msg)


def _should_try_gmail_ssl_fallback(host: str, port: int, use_ssl: bool, use_tls: bool, exc: Exception) -> bool:
    if use_ssl or not use_tls or port != 587:
        return False
    if "gmail.com" not in host.lower():
        return False
    return isinstance(exc, (TimeoutError, smtplib.SMTPServerDisconnected))


def _send_email(subject: str, body: str) -> None:
    host = env_optional("SMTP_HOST", "")
    port = int(env_optional("SMTP_PORT", "587"))
    username = env_optional("SMTP_USERNAME", "")
    password = env_optional("SMTP_PASSWORD", "")
    use_ssl = env_optional("SMTP_USE_SSL", "false").lower() == "true"
    use_tls = env_optional("SMTP_USE_TLS", "true").lower() == "true"
    msg, _from_addr, _to_addrs = _build_message(subject, body)

    primary_error: Exception | None = None
    try:
        if use_ssl:
            _send_via_ssl(host, port, username, password, msg)
        else:
            _send_via_starttls(host, port, username, password, msg)
        return
    except Exception as exc:
        primary_error = exc
        if not _should_try_gmail_ssl_fallback(host, port, use_ssl, use_tls, exc):
            raise

    try:
        _send_via_ssl(host, 465, username, password, msg)
    except Exception as fallback_exc:
        raise RuntimeError(
            f"Primary SMTP delivery failed ({primary_error}); Gmail SSL fallback also failed ({fallback_exc})."
        ) from fallback_exc


def _send_wecom(webhook_url: str, body: str) -> None:
    content = clean_text(body)
    if len(content) > 1800:
        content = content[:1800] + " ..."
    session = make_session(with_retry=False)
    response = session.post(
        webhook_url,
        json={"msgtype": "text", "text": {"content": content}},
        timeout=30,
    )
    response.raise_for_status()
