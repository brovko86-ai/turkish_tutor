"""Опрос Telegram: ищем команду /today, если есть — запускаем генерацию."""

from __future__ import annotations

import os
import sys

import common
import generate


def main() -> int:
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    offset = common.load_offset()

    updates = common.get_updates(offset=offset, timeout=0)
    if not updates:
        return 0

    max_update_id = offset - 1 if offset else 0
    today_requested = False

    for upd in updates:
        update_id = upd.get("update_id", 0)
        if update_id > max_update_id:
            max_update_id = update_id

        message = upd.get("message") or upd.get("edited_message") or {}
        msg_chat = str((message.get("chat") or {}).get("id", ""))
        if msg_chat != chat_id:
            continue

        text = (message.get("text") or "").strip()
        if text.split()[:1] == ["/today"]:
            today_requested = True

    common.save_offset(max_update_id + 1)

    if today_requested:
        print("[poll] обнаружена /today — запускаю генерацию")
        return generate.generate(mode="on_demand")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        try:
            common.send_message(f"poll_telegram: сбой: {exc}")
        except Exception:
            pass
        print(f"[error] {exc}", file=sys.stderr)
        raise
