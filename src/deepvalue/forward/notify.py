"""
Operational notifications + heartbeat for the unattended forward clock (ported from parley;
requests -> httpx to match deepvalue's HTTP client).

Two host-agnostic, env-configured concerns so they behave identically on a laptop and inside
the deployment container:

- **Alert** (`notify`): a one-line success/failure ping per weekly run, so a failed session is
  *loud* instead of a silent hole in the forward track record. Dispatches to every configured
  channel — Telegram (TELEGRAM_BOT_TOKEN/CHAT_ID; instant phone push) and/or email (SMTP env).
  A no-op (logged, never raised) when none is configured.
- **Heartbeat** (`write_heartbeat`/`read_heartbeat`/`heartbeat_stale`): a tiny JSON record of
  the last run's time + status, so "has the clock gone quiet?" is a cheap, pollable check.

Notifications must never break a run: every path swallows its own errors.
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

DEFAULT_HEARTBEAT_PATH = Path("data/forward/last_run.json")


def _smtp_config() -> dict | None:
    """SMTP settings from env, or None (email no-op). Required: SMTP_HOST, SMTP_USER,
    SMTP_PASSWORD, ALERT_EMAIL_TO. Optional: SMTP_PORT (587), SMTP_FROM (=SMTP_USER).
    Port 465 -> implicit TLS; else STARTTLS."""
    host, user = os.getenv("SMTP_HOST"), os.getenv("SMTP_USER")
    password, to = os.getenv("SMTP_PASSWORD"), os.getenv("ALERT_EMAIL_TO")
    if not (host and user and password and to):
        return None
    return {"host": host, "port": int(os.getenv("SMTP_PORT", "587")), "user": user,
            "password": password, "from": os.getenv("SMTP_FROM", user), "to": to}


def send_email(subject: str, body: str) -> bool:
    """Send a plaintext alert email. Never raises; logged no-op when SMTP env absent."""
    cfg = _smtp_config()
    if cfg is None:
        logger.info("email alert skipped: SMTP not configured")
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, cfg["from"], cfg["to"]
    msg.set_content(body)
    try:
        if cfg["port"] == 465:
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30) as s:
                s.login(cfg["user"], cfg["password"]); s.send_message(msg)
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as s:
                s.starttls(); s.login(cfg["user"], cfg["password"]); s.send_message(msg)
        logger.info("alert email sent to %s: %s", cfg["to"], subject)
        return True
    except Exception as e:  # noqa: BLE001 — notifications never break the run
        logger.warning("alert email failed (%s: %s)", type(e).__name__, e)
        return False


def send_telegram(text: str) -> bool:
    """Send via the Telegram Bot API (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID). Never raises."""
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        logger.info("telegram alert skipped: TELEGRAM_BOT_TOKEN/CHAT_ID not set")
        return False
    try:
        r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
                       timeout=15)
        if r.status_code == 200:
            logger.info("telegram alert sent")
            return True
        logger.warning("telegram alert failed: HTTP %s %s", r.status_code, r.text[:200])
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning("telegram alert failed (%s: %s)", type(e).__name__, e)
        return False


def notify(subject: str, body: str = "") -> bool:
    """Alert over every configured channel (Telegram and/or email). True if any delivered."""
    text = f"{subject}\n\n{body}" if body else subject
    sent = send_telegram(text)
    sent = send_email(subject, body) or sent
    if not sent:
        logger.info("no alert channel configured (set TELEGRAM_* and/or SMTP_*)")
    return sent


@dataclass(frozen=True)
class Heartbeat:
    status: str   # "ok" | "error"
    as_of: str    # the session's decision date
    ts: str       # wall-clock UTC ISO timestamp
    note: str = ""


def write_heartbeat(status: str, as_of: str, note: str = "",
                    path: Path = DEFAULT_HEARTBEAT_PATH) -> None:
    """Record the last run's outcome. Best-effort — never raises."""
    hb = Heartbeat(status=status, as_of=as_of, ts=datetime.now(UTC).isoformat(), note=note[:500])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(hb), indent=2))
    except Exception as e:  # noqa: BLE001
        logger.warning("heartbeat write failed: %s", e)


def read_heartbeat(path: Path = DEFAULT_HEARTBEAT_PATH) -> Heartbeat | None:
    if not path.exists():
        return None
    try:
        return Heartbeat(**json.loads(path.read_text()))
    except Exception as e:  # noqa: BLE001
        logger.warning("heartbeat read failed: %s", e)
        return None


def heartbeat_stale(hb: Heartbeat | None, max_age_hours: float) -> bool:
    """True if no heartbeat, the last run errored, or it's older than allowed (clock quiet)."""
    if hb is None or hb.status != "ok":
        return True
    try:
        age = datetime.now(UTC) - datetime.fromisoformat(hb.ts)
    except ValueError:
        return True
    return age.total_seconds() > max_age_hours * 3600
