"""Опрос Telegram: команды от ученика.

Поддерживаемые команды:
- `/today`                   — запустить on-demand генерацию тренировки.
- `/topics <тема1>, <тема2>` — записать слабые темы в weak_topics
                                (узнаются на диагностическом экзамене).
- `/learned <слово1> <слово2>` — убрать слова из weak_words
                                (ученик закрыл их в тренажёре).
- `/clear topics`            — очистить весь weak_topics.
- `/clear weak`              — очистить весь weak_words.
- `/status`                  — текущий прогресс (lesson, sessions, weak counts).
- `/help`                    — список команд.

Любая команда отправляет короткое подтверждение в Telegram, чтобы было
видно, что бот её принял.
"""

from __future__ import annotations

import os
import re
import sys

import common
import generate


HELP_TEXT = (
    "Доступные команды:\n"
    "  /today — сгенерировать тренировку прямо сейчас\n"
    "  /weak <слово> <слово> — добавить турецкие слова в weak_words\n"
    "  /learned <слово> <слово> — убрать слова из weak_words\n"
    "  /topics <тема>, <тема> — записать слабые темы (с экзамена)\n"
    "  /clear topics — очистить weak_topics\n"
    "  /clear weak — очистить weak_words\n"
    "  /status — текущий прогресс\n"
    "  /help — этот список"
)


def _cmd_topics(args: str) -> str:
    """`/topics t1, t2, t3` → положить в weak_topics."""
    topics = [t.strip() for t in args.split(",") if t.strip()]
    if not topics:
        return "⚠️ /topics: укажи список тем через запятую."
    progress = common.load_progress()
    progress["weak_topics"] = topics
    common.save_progress(progress)
    return (
        f"✅ weak_topics обновлены ({len(topics)} тем). "
        f"Со следующей тренировки Recall начнёт прицельно бить:\n"
        + "\n".join(f"  • {t}" for t in topics)
    )


def _cmd_weak(args: str) -> str:
    """`/weak word1 word2` или `/weak word1, word2` — добавить в weak_words.
    Совместимо со старой семантикой бота: ученик отмечает, что эти слова
    забывает. Перевод модель подставит сама на следующей тренировке
    (берём ru из active/long_term, если есть; иначе оставляем пустой).
    Дубли не создаём."""
    raw = [w.strip() for w in re.split(r"[,\s]+", args) if w.strip()]
    if not raw:
        return "⚠️ /weak: укажи турецкие слова через пробел или запятую."
    progress = common.load_progress()
    weak = progress.get("weak_words", []) or []
    existing = {(w.get("tr") or "").strip().lower() for w in weak}
    # ищем перевод в active/long_term
    bank = progress.get("vocabulary_bank", {}) or {}
    all_known = (bank.get("active", []) or []) + (bank.get("long_term", []) or [])
    ru_by_tr = {(w.get("tr") or "").strip().lower(): w.get("ru", "")
                for w in all_known if w.get("tr")}

    added = []
    skipped = []
    today = common.today_str()
    for w in raw:
        key = w.lower()
        if key in existing:
            skipped.append(w)
            continue
        weak.append({
            "tr": w,
            "ru": ru_by_tr.get(key, ""),
            "added": today,
            "fails": 1,
        })
        existing.add(key)
        added.append(w)

    progress["weak_words"] = weak
    common.save_progress(progress)
    parts = []
    if added:
        parts.append(f"✅ Добавил {len(added)} в weak_words: {', '.join(added)}")
    if skipped:
        parts.append(f"⏭ Уже были в weak_words: {', '.join(skipped)}")
    parts.append(f"Всего сейчас: {len(weak)} weak_words.")
    return "\n".join(parts)


def _cmd_learned(args: str) -> str:
    """`/learned word1 word2` или `/learned word1, word2` — убрать из weak_words."""
    raw = [w.strip().lower() for w in re.split(r"[,\s]+", args) if w.strip()]
    if not raw:
        return "⚠️ /learned: укажи турецкие слова через пробел или запятую."
    progress = common.load_progress()
    weak = progress.get("weak_words", []) or []
    before = len(weak)
    target = set(raw)
    kept = [w for w in weak if (w.get("tr") or "").strip().lower() not in target]
    removed = before - len(kept)
    progress["weak_words"] = kept
    common.save_progress(progress)
    if removed == 0:
        return (
            f"⚠️ Ни одно из присланных слов не было в weak_words. "
            f"Сейчас там: " + ", ".join((w.get('tr') or '?') for w in weak[:10])
        )
    return (
        f"✅ Убрал {removed} слов из weak_words ({before} → {len(kept)}). "
        f"Закрытые: {', '.join(sorted(target))}"
    )


def _cmd_clear(args: str) -> str:
    target = args.strip().lower()
    progress = common.load_progress()
    if target == "topics":
        n = len(progress.get("weak_topics", []) or [])
        progress["weak_topics"] = []
        common.save_progress(progress)
        return f"✅ weak_topics очищены ({n} тем удалено)."
    if target == "weak":
        n = len(progress.get("weak_words", []) or [])
        progress["weak_words"] = []
        common.save_progress(progress)
        return f"✅ weak_words очищены ({n} слов удалено)."
    return "⚠️ /clear: укажи `topics` или `weak`."


def _cmd_status(_args: str) -> str:
    p = common.load_progress()
    return (
        f"📊 Текущий прогресс\n"
        f"Урок: {p.get('current_lesson')} «{p.get('lesson_title')}»\n"
        f"Session: {p.get('session_number')}, mode: {p.get('mode', 'curriculum')}\n"
        f"Active (5 свежих слов): "
        f"{', '.join(w['tr'] for w in p.get('vocabulary_bank',{}).get('active',[]))}\n"
        f"Long-term: {len(p.get('vocabulary_bank',{}).get('long_term',[]))} слов\n"
        f"Weak words: {len(p.get('weak_words',[]) or [])}\n"
        f"Weak topics: {len(p.get('weak_topics',[]) or [])}"
    )


# command → handler. Возвращает текст для отправки в Telegram (или "" если ничего).
COMMANDS = {
    "/weak":    _cmd_weak,
    "/learned": _cmd_learned,
    "/topics":  _cmd_topics,
    "/clear":   _cmd_clear,
    "/status":  _cmd_status,
}


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
        if not text.startswith("/"):
            continue

        # /today обрабатываем после всех апдейтов (один запуск генерации).
        if text.split()[:1] == ["/today"]:
            today_requested = True
            continue

        if text.split()[:1] == ["/help"]:
            common.send_message(HELP_TEXT)
            continue

        # /cmd args ...
        cmd, _, args = text.partition(" ")
        handler = COMMANDS.get(cmd.lower())
        if handler:
            try:
                reply = handler(args)
                common.send_message(reply)
                print(f"[poll] {cmd}: {reply[:80]}")
            except Exception as exc:
                common.send_message(f"⚠️ {cmd} упал: {exc}")
                print(f"[poll] {cmd} error: {exc}", file=sys.stderr)

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
