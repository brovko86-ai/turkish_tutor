"""Главный сценарий: одна генерация тренировки.

Запускается как:
    RUN_MODE=scheduled  python scripts/generate.py    # из daily.yml
    RUN_MODE=on_demand  python scripts/generate.py    # из poll_telegram.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import anthropic

import common


MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = int(os.environ.get("ANTHROPIC_MAX_TOKENS", "32000"))
NEW_WORDS_PER_DAY = 5

MANIFEST_RE = re.compile(r"<manifest>\s*(\{.*?\})\s*</manifest>", re.DOTALL)
HTML_RE = re.compile(r"<html\b.*?</html\s*>", re.DOTALL | re.IGNORECASE)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _style_sample() -> str:
    """Краткая выжимка стиля: ссылка на последнюю тренировку как образец.

    Передаём только последний файл и обрезаем его до разумного размера,
    чтобы не раздувать контекст.
    """
    sample = common.latest_training()
    if sample is None:
        return "(образцов предыдущих тренировок ещё нет)"
    text = sample.read_text(encoding="utf-8")
    # Достаточно первых ~8000 символов: модель видит структуру и тон.
    return f"Файл: {sample.name}\n\n{text[:8000]}"


def _lesson_index() -> list[tuple[str, str]]:
    """Список (имя файла, первая значимая строка с заголовком) всех материалов.

    Используется как индекс для модели: даже если матч по номеру не
    совпал с темой, модель может попросить (или сослаться) на другой
    конспект курса.
    """
    materials_dir = common.REPO_ROOT / "lesson_materials"
    if not materials_dir.exists():
        return []
    out: list[tuple[str, str]] = []
    for path in sorted(materials_dir.iterdir()):
        if path.suffix not in (".md", ".txt") or path.name == "README.md":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        # первая строка вида "# Lesson NN — TITLE" или просто первая непустая
        title = ""
        for line in text.splitlines():
            s = line.strip().lstrip("#").strip()
            if s:
                title = s
                break
        out.append((path.name, title[:120]))
    return out


def _lesson_material(lesson_number: int, lesson_title: str) -> tuple[str, str]:
    """Возвращает (контент_материала, заметка_для_модели).

    Стратегия (жёсткая):
    1. Если файла `lesson_NN.md` нет — пусто, без шума.
    2. Файл есть, ключевые слова из `lesson_title` встречаются в тексте —
       уверенный матч, передаём контент.
    3. Файл есть, но тема НЕ совпадает (Cowork-курс был переструктурирован) —
       **не передаём контент вообще**, возвращаем только заметку, чтобы
       модель не соблазнилась подменить тему урока на ту, что в конспекте.
       Раньше передавали с warning'ом, но эксперимент показал, что модель
       его игнорирует и идёт за контентом.
    """
    materials_dir = common.REPO_ROOT / "lesson_materials"
    if not materials_dir.exists():
        return ("", "")
    stem = f"lesson_{lesson_number:02d}"
    for ext in (".md", ".txt"):
        candidate = materials_dir / f"{stem}{ext}"
        if not candidate.exists():
            continue
        text = candidate.read_text(encoding="utf-8")
        # ключевые слова темы — латиница/кириллица длиной ≥4
        title_tokens = {
            tok.strip(".,()-").lower()
            for tok in lesson_title.split()
            if len(tok.strip(".,()-")) >= 4
        }
        text_lower = text.lower()
        matches = sum(1 for t in title_tokens if t in text_lower)
        if title_tokens and matches == 0:
            # mismatch: НЕ передаём текст файла, чтобы не сбить модель
            note = (
                f"📌 По номеру урока ({lesson_number}) в `lesson_materials/` "
                f"есть конспект `{candidate.name}`, но его тема не совпадает "
                f"с текущей темой ученика «{lesson_title}» — Cowork-курс был "
                f"переструктурирован после написания материалов. Конспект "
                f"намеренно не передан, чтобы не сбить тему. Опирайся на "
                f"`lesson_title` из `lesson_progress.json` и собственное "
                f"знание турецкой грамматики."
            )
            return ("", note)
        # match — передаём контент
        return (f"Файл: {candidate.name}\n\n{text}", "")
    return ("", "")


def _known_words(progress: dict) -> set[str]:
    """Множество всех турецких слов, которые уже знакомы ученику —
    active, long_term и weak_words. Используется и для подсказки модели,
    и для валидации `new_words` после ответа."""
    bank = progress.get("vocabulary_bank", {}) or {}
    out: set[str] = set()
    for bucket in (bank.get("active", []), bank.get("long_term", [])):
        for w in bucket or []:
            tr = (w or {}).get("tr")
            if tr:
                out.add(tr.strip().lower())
    for w in progress.get("weak_words", []) or []:
        tr = (w or {}).get("tr")
        if tr:
            out.add(tr.strip().lower())
    return out


def _build_user_message(today: str, progress: dict, style: str,
                        lesson_material: str, material_note: str,
                        lesson_index: list[tuple[str, str]]) -> str:
    progress_json = json.dumps(progress, ensure_ascii=False, indent=2)
    lesson_title = progress.get("lesson_title", "(не задано)")
    lesson_number = progress.get("current_lesson", "?")
    known = sorted(_known_words(progress))

    # Жёсткое утверждение про источник темы — первая важная вещь, которую
    # видит модель после даты.
    topic_anchor = (
        f"\n**ТЕМА УРОКА — ЕДИНСТВЕННЫЙ ИСТОЧНИК ИСТИНЫ:** "
        f"`lesson_title = «{lesson_title}»` из JSON ниже. "
        f"Сегодня сгенерируй тренировку именно по этой теме. "
        f"Не подменяй её другой темой, даже если в конспектах курса под "
        f"тем же номером урока {lesson_number} лежит что-то иное.\n"
    )

    # Запрет на повторы: все 5 новых слов в манифесте обязаны быть
    # незнакомыми. Иначе ученик «учит» то, что уже было.
    forbidden_block = ""
    if known:
        forbidden_block = (
            f"\n**ЗАПРЕТ НА ПОВТОР ЛЕКСИКИ:** в `manifest.new_words` "
            f"должно быть 5 турецких слов, которых **нет** в этом списке "
            f"уже знакомых ученику слов ({len(known)} штук):\n"
            f"```\n{', '.join(known)}\n```\n"
            f"Эти слова можно использовать в упражнениях и тексте "
            f"тренировки (для повторения), но они **не должны** попадать "
            f"в `new_words`. Если ты случайно положишь повтор — генерация "
            f"будет отвергнута и ученик ничего не получит.\n"
        )

    material_block = ""
    if lesson_material:
        material_block = (
            f"\nКонспект из исходных материалов курса (тема совпала, "
            f"опирайся на формулы, таблицы и примеры отсюда):\n```\n"
            f"{lesson_material}\n```\n"
        )
    elif material_note:
        material_block = f"\n{material_note}\n"

    index_block = ""
    if lesson_index:
        lines = "\n".join(f"- {name} — {title}" for name, title in lesson_index)
        index_block = (
            f"\nДоступные конспекты курса (для справки — можно мысленно "
            f"сослаться, если нужна база по более ранней теме):\n{lines}\n"
        )

    return f"""Сгенерируй тренировку на сегодня — {today}.
{topic_anchor}{forbidden_block}
Текущий прогресс ученика (`lesson_progress.json`):
```json
{progress_json}
```
{material_block}{index_block}
Образец стиля прошлой тренировки (только для тона и структуры; **не копируй
содержание**):
```
{style}
```

Контракт вывода — **строго** в таком порядке:

1. Сначала блок `<manifest>...</manifest>` c JSON следующего вида:
```json
{{
  "lesson_number": <число>,
  "lesson_title": "<строка>",
  "session_number": <число>,
  "is_new_lesson": <true|false>,
  "new_words": [
    {{"tr": "...", "ru": "..."}},
    ... ровно {NEW_WORDS_PER_DAY} элементов
  ]
}}
```

2. Сразу за ним — полный файл тренировки в блоке `<html>...</html>`
   (валидный самодостаточный HTML со встроенными стилями и скриптами,
   все 8 блоков из `generation_rules.md`).

Никакого текста до `<manifest>` и после `</html>`. JSON-манифест должен
быть валиден и содержать ровно {NEW_WORDS_PER_DAY} новых слов."""


def _parse_response(text: str) -> tuple[dict, str]:
    m_manifest = MANIFEST_RE.search(text)
    if not m_manifest:
        raise ValueError("в ответе нет блока <manifest>...</manifest>")
    try:
        manifest = json.loads(m_manifest.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(f"невалидный JSON в манифесте: {exc}") from exc

    m_html = HTML_RE.search(text)
    if not m_html:
        raise ValueError("в ответе нет блока <html>...</html>")
    html = m_html.group(0).strip()
    if len(html) < 1000:
        raise ValueError(f"HTML подозрительно короткий: {len(html)} символов")

    required = {"lesson_number", "lesson_title", "session_number",
                "is_new_lesson", "new_words"}
    missing = required - manifest.keys()
    if missing:
        raise ValueError(f"в манифесте нет полей: {sorted(missing)}")
    if not isinstance(manifest["new_words"], list) \
            or len(manifest["new_words"]) != NEW_WORDS_PER_DAY:
        raise ValueError(
            f"new_words должно быть списком из {NEW_WORDS_PER_DAY} элементов, "
            f"получено: {manifest.get('new_words')!r}"
        )
    for w in manifest["new_words"]:
        if not isinstance(w, dict) or "tr" not in w or "ru" not in w:
            raise ValueError(f"элемент new_words без полей tr/ru: {w!r}")

    return manifest, html


def _apply_progress(progress: dict, manifest: dict, today: str) -> dict:
    """Детерминированно обновляем lesson_progress.json по манифесту."""
    bank = progress.setdefault("vocabulary_bank", {})
    active = bank.get("active", []) or []
    long_term = bank.get("long_term", []) or []

    for word in active:
        word = dict(word)
        word["moved"] = today
        long_term.append(word)

    bank["active"] = [
        {"tr": w["tr"], "ru": w["ru"], "introduced": today}
        for w in manifest["new_words"]
    ]
    bank["long_term"] = long_term

    progress["current_lesson"] = manifest["lesson_number"]
    progress["lesson_title"] = manifest["lesson_title"]
    progress["session_number"] = manifest["session_number"]
    progress["date"] = today

    if manifest.get("is_new_lesson"):
        completed = progress.setdefault("completed_lessons", [])
        if manifest["lesson_number"] not in completed:
            completed.append(manifest["lesson_number"])
        progress["next_lesson"] = manifest["lesson_number"] + 1

    return progress


def generate(mode: str) -> int:
    today = common.today_str()
    out_path = common.training_path(today)

    if out_path.exists():
        if mode == "scheduled":
            print(f"[scheduled] {out_path.name} уже существует, выхожу.")
            return 0
        # on_demand: пере-отправляем готовый файл
        print(f"[on_demand] {out_path.name} уже существует, отправляю заново.")
        common.send_document(out_path, caption="Тренировка за сегодня уже готова.")
        return 0

    tutor_prompt = _read(common.REPO_ROOT / "tutor_prompt.md")
    vocab_spec = _read(common.REPO_ROOT / "VOCAB_TRAINER_SPEC.md")
    rules = _read(common.REPO_ROOT / "generation_rules.md")
    progress = common.load_progress()
    style = _style_sample()
    material, note = _lesson_material(
        int(progress.get("current_lesson", 0)),
        str(progress.get("lesson_title", "")),
    )
    index = _lesson_index()

    user_message = _build_user_message(today, progress, style, material, note, index)

    client = anthropic.Anthropic()
    known = _known_words(progress)

    def _call(extra_user_msg: str = "") -> tuple[dict, str]:
        """Один вызов API + парсинг. Возвращает (manifest, html) или
        бросает ValueError при невалидной структуре / повторах."""
        msg = user_message + (("\n\n" + extra_user_msg) if extra_user_msg else "")
        chunks: list[str] = []
        with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[
                {"type": "text", "text": tutor_prompt},
                {"type": "text", "text": vocab_spec + "\n\n" + rules},
            ],
            messages=[{"role": "user", "content": msg}],
        ) as stream:
            for delta in stream.text_stream:
                chunks.append(delta)
        text = "".join(chunks)
        print(f"[anthropic] получено {len(text)} символов")
        m, h = _parse_response(text)
        reps = [
            w for w in m["new_words"]
            if w["tr"].strip().lower() in known
        ]
        if reps:
            raise ValueError(
                "повтор знакомой лексики: "
                + ", ".join(f"{w['tr']!r}" for w in reps)
            )
        return m, h

    # До 2 попыток: при первом провале даём явный «не используй эти слова».
    # Тема урока могла ограничить пул кандидатов, и модель сама не догадалась
    # взять менее очевидные варианты.
    manifest: dict | None = None
    html: str = ""
    last_error: str = ""
    for attempt in (1, 2):
        try:
            manifest, html = _call(
                "" if attempt == 1 else
                f"⚠️ Предыдущая попытка отклонена: {last_error}. "
                f"Сгенерируй заново, целиком — с теми же темой и структурой, "
                f"но **другие 5 слов в `new_words`**, которых **точно нет** в "
                f"списке уже знакомых ученику слов (см. ЗАПРЕТ выше). Бери "
                f"менее очевидные варианты, расширяющие словарный запас."
            )
            break
        except ValueError as exc:
            last_error = str(exc)
            print(f"[retry] попытка {attempt} провалена: {exc}", file=sys.stderr)
    if manifest is None:
        common.send_message(
            f"Тренировка {today}: после 2 попыток модель так и не дала "
            f"валидный ответ. Последняя ошибка: {last_error}"
        )
        return 2

    common.TRAININGS_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html + "\n", encoding="utf-8")

    updated = _apply_progress(progress, manifest, today)
    common.save_progress(updated)

    caption = (
        f"Урок {manifest['lesson_number']}: {manifest['lesson_title']}"
        f" (сессия {manifest['session_number']})"
    )
    common.send_document(out_path, caption=caption)
    print(f"[ok] {out_path.name} сгенерирован и отправлен")
    return 0


def main() -> int:
    mode = os.environ.get("RUN_MODE", "scheduled")
    if mode not in {"scheduled", "on_demand"}:
        print(f"неизвестный RUN_MODE={mode}", file=sys.stderr)
        return 64
    try:
        return generate(mode)
    except Exception as exc:
        try:
            common.send_message(f"Тренировка: сбой генерации ({mode}): {exc}")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    raise SystemExit(main())
