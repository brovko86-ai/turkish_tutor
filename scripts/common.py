"""Общие хелперы: Telegram-API, загрузка/сохранение состояния."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests


REPO_ROOT = Path(__file__).resolve().parent.parent
PROGRESS_PATH = REPO_ROOT / "lesson_progress.json"
OFFSET_PATH = REPO_ROOT / "state" / "telegram_offset.txt"
TRAININGS_DIR = REPO_ROOT / "trainings"

# Часовой пояс генерации (Europe/Moscow, UTC+3, без переходов).
LOCAL_TZ = timezone(timedelta(hours=3))


def today_str() -> str:
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")


def training_path(date_str: str) -> Path:
    return TRAININGS_DIR / f"training_{date_str}.html"


# --- Telegram ---------------------------------------------------------------


def _bot_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")
    return token


def _chat_id() -> str:
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID не задан")
    return chat_id


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{_bot_token()}/{method}"


def _check_ok(method: str, resp: requests.Response) -> dict:
    """HTTP 200 + ok:true в теле. Telegram при ошибке часто возвращает 200
    с {"ok": false, "description": "..."} — без этой проверки сбой
    маскируется под успех."""
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("ok"):
        raise RuntimeError(
            f"Telegram {method} failed: "
            f"{payload.get('description') or payload}"
        )
    return payload


def send_message(text: str) -> None:
    resp = requests.post(
        _api("sendMessage"),
        data={"chat_id": _chat_id(), "text": text},
        timeout=30,
    )
    _check_ok("sendMessage", resp)


def send_document(path: Path, caption: str | None = None) -> None:
    with path.open("rb") as fh:
        data = {"chat_id": _chat_id()}
        if caption:
            data["caption"] = caption
        resp = requests.post(
            _api("sendDocument"),
            data=data,
            files={"document": (path.name, fh, "text/html")},
            timeout=120,
        )
    payload = _check_ok("sendDocument", resp)
    msg_id = (payload.get("result") or {}).get("message_id")
    if msg_id:
        print(f"[telegram] sendDocument ok, message_id={msg_id}")


def get_updates(offset: int, timeout: int = 0) -> list[dict]:
    resp = requests.get(
        _api("getUpdates"),
        params={"offset": offset, "timeout": timeout},
        timeout=30,
    )
    return _check_ok("getUpdates", resp).get("result", [])


# --- Локальные файлы --------------------------------------------------------


def load_progress() -> dict:
    return json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))


def save_progress(progress: dict) -> None:
    PROGRESS_PATH.write_text(
        json.dumps(progress, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_offset() -> int:
    if not OFFSET_PATH.exists():
        return 0
    raw = OFFSET_PATH.read_text(encoding="utf-8").strip()
    return int(raw) if raw else 0


def save_offset(offset: int) -> None:
    OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_PATH.write_text(str(offset) + "\n", encoding="utf-8")


def latest_training() -> Path | None:
    if not TRAININGS_DIR.exists():
        return None
    files = sorted(TRAININGS_DIR.glob("training_*.html"))
    return files[-1] if files else None
