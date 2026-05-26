"""Одноразовый мини-экзамен по пройденным темам.

Запускается:
    .venv/bin/python scripts/audit_exam.py

Что делает:
- Читает `lesson_progress.json`, `tutor_prompt.md`, индекс `lesson_materials/`.
- Просит Anthropic API сгенерить интерактивный HTML-экзамен по 24 темам
  курса (уроки 8-32), ~3-4 задачи на тему.
- В конце HTML — встроенная JS-сводка по темам (% правильных) и
  копируемая команда для бота вида `/weak <тема1>, <тема2>, ...`.
- HTML сохраняется в `audits/exam_YYYY-MM-DD.html` и отправляется в Telegram.

После прохождения ученик присылает в чат ассистенту список слабых тем,
которые добавляются в `lesson_progress.json` как `weak_topics`.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import anthropic

import common
import generate as gen   # переиспользуем _read, _parse_response, MODEL и пр.


AUDITS_DIR = common.REPO_ROOT / "audits"
MANIFEST_RE = re.compile(r"<manifest>\s*(\{.*?\})\s*</manifest>", re.DOTALL)
HTML_RE = re.compile(r"<html\b.*?</html\s*>", re.DOTALL | re.IGNORECASE)


def _build_exam_user_message(progress: dict, lesson_index: list[tuple[str, str]]) -> str:
    progress_json = json.dumps(progress, ensure_ascii=False, indent=2)
    completed = sorted(progress.get("completed_lessons", []) or [])
    index_lines = "\n".join(f"- {name} — {title}" for name, title in lesson_index)
    return f"""Сгенерируй **диагностический мини-экзамен** по уже пройденным темам
турецкого курса. Цель — понять, в каких темах у ученика реальные провалы,
а в каких всё хорошо. По результатам слабые темы попадут в weak_topics и
будут приоритетно появляться в Recall будущих тренировок.

## Прогресс ученика

```json
{progress_json}
```

## Список доступных конспектов курса (откуда брать темы)

{index_lines}

## Что должно быть в экзамене

- Покрой темы из `completed_lessons` (={completed}).
- На каждую тему — **2 разнотипные задачи** (например: одна на
  распознавание формы — multiple choice, одна на применение — вставь
  суффикс в предложение, ИЛИ перевод короткой фразы TR↔RU). Не
  растягивай в три, делай ровно две — чтобы экзамен прошёлся за один
  присест.
- Итого **~24 темы × 2 задачи ≈ 45-50 вопросов**. Раздели на 4 секции
  по нарастанию сложности (Базовая база → Падежи → Времена →
  Изафеты и сложные формы).
- **Никаких новых тем** — только то, что уже в `completed_lessons`.

## Технические требования к HTML

- Один самодостаточный файл с инлайн CSS+JS, в стиле обычных тренировок
  (тёмный фон #0f1117, акцент на #f39c12, моноспейс для формул).
- Каждый вопрос содержит атрибут `data-topic="..."` с **коротким
  идентификатором темы** (например, `"locative -de/-da"`, `"past -DI"`,
  `"definite-compound"`). По нему JS будет считать % правильных по теме.
- Поддерживаемые типы вопросов:
  - Multiple choice (4 варианта) — кнопки `.mc-btn`.
  - Текстовое поле + кнопка «Проверить» — `<input type="text">` +
    `.check-btn`. Допустимы синонимы (несколько правильных ответов через `|`).
- После каждого ответа — мгновенный feedback: подсветка
  правильного/неправильного, краткое объяснение (1-2 строки), почему
  ответ такой.
- В самом конце страницы — блок «📊 Сводка по темам». JS обходит все
  вопросы по `data-topic`, считает score = correct / total для каждой
  темы, выводит таблицу:
  - 🟢 зелёный — score ≥ 80%
  - 🟡 жёлтый — 50-80%
  - 🔴 красный — < 50%
- Под таблицей — **копируемая команда для бота**:
  `/weak topic1, topic2, topic3` (только темы с красным и жёлтым).
  Команда формируется JS после прохождения, есть кнопка «📋 Скопировать».

## Контракт вывода (как у обычной тренировки)

Сначала блок `<manifest>...</manifest>` со ВРЕМЕННЫМИ метаданными
(чтобы переиспользовать парсер `_parse_response`):

```json
{{
  "lesson_number": 0,
  "lesson_title": "Диагностический экзамен",
  "session_number": 1,
  "is_new_lesson": false,
  "new_words": []
}}
```

(`new_words` — пустой массив; это не учебная тренировка, прогресс
обновлять не нужно.)

Сразу за манифестом — полный HTML-файл в `<html>...</html>`.

Никакого текста до `<manifest>` и после `</html>`. Длина HTML
ожидается ~80-120 KB."""


def _parse_exam(text: str) -> str:
    """Лёгкий парсер: только HTML. Манифест проверяем для совместимости."""
    m_html = HTML_RE.search(text)
    if not m_html:
        raise ValueError("в ответе нет блока <html>...</html>")
    html = m_html.group(0).strip()
    if len(html) < 5000:
        raise ValueError(
            f"HTML экзамена подозрительно короткий: {len(html)} символов"
        )
    return html


def main() -> int:
    today = common.today_str()
    AUDITS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = AUDITS_DIR / f"exam_{today}.html"

    if out_path.exists():
        print(f"[exam] {out_path.name} уже существует, переотправляю.")
        common.send_document(
            out_path,
            caption="Диагностический экзамен (повторная отправка).",
        )
        return 0

    tutor_prompt = gen._read(common.REPO_ROOT / "tutor_prompt.md")
    vocab_spec = gen._read(common.REPO_ROOT / "VOCAB_TRAINER_SPEC.md")
    rules = gen._read(common.REPO_ROOT / "generation_rules.md")
    progress = common.load_progress()
    index = gen._lesson_index()
    user_message = _build_exam_user_message(progress, index)

    # Большие HTML-генерации могут идти >10 минут по сети.
    # Дефолтный httpx timeout обрывает стрим — поднимаем до 30 минут.
    client = anthropic.Anthropic(timeout=1800.0)
    chunks: list[str] = []
    with client.messages.stream(
        model=gen.MODEL,
        max_tokens=gen.MAX_TOKENS,
        system=[
            {"type": "text", "text": tutor_prompt},
            {"type": "text", "text": vocab_spec + "\n\n" + rules},
        ],
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for delta in stream.text_stream:
            chunks.append(delta)
    text = "".join(chunks)
    print(f"[anthropic] получено {len(text)} символов")

    try:
        html = _parse_exam(text)
    except ValueError as exc:
        common.send_message(f"Экзамен: ошибка генерации.\n{exc}")
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    out_path.write_text(html + "\n", encoding="utf-8")
    common.send_document(
        out_path,
        caption=(
            "📊 Диагностический мини-экзамен по пройденным темам. "
            "Пройди, посмотри сводку, пришли мне (ассистенту) список "
            "слабых тем — добавлю их в weak_topics."
        ),
    )
    print(f"[ok] {out_path.name} сгенерирован и отправлен")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
