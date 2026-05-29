"""Одноразовый мини-экзамен по пройденным темам.

Запускается:
    .venv/bin/python scripts/audit_exam.py

Что делает:
- Двумя API-вызовами генерирует HTML-блоки заданий: «база» (уроки 8–18)
  и «продвинутая часть» (уроки 19–32) — так каждый запрос укладывается в
  ~3–5 минут без таймаутов.
- Склеивает их в один самодостаточный HTML с общим JS-движком: подсчёт
  правильных, сводка по темам в конце (🟢🟡🔴) и копируемая команда
  `/topics ...` для бота.
- Сохраняет в `audits/exam_YYYY-MM-DD.html` и отправляет в Telegram.

После прохождения ученик присылает в чат список слабых тем — они
ложатся в `lesson_progress.json.weak_topics`.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import anthropic

import common
import generate as gen


AUDITS_DIR = common.REPO_ROOT / "audits"
QUESTIONS_RE = re.compile(r"<questions>(.*?)</questions>", re.DOTALL | re.IGNORECASE)


# Описание двух частей. Темы привязаны к Cowork-плану из tutor_prompt.md.
PARTS = [
    {
        "name": "Часть 1 · База",
        "topics_hint": (
            "падежи (-DE/-DA локатив, -E/-A датив, -DEN/-DAN аблатив, "
            "-LE/-LA инструменталис, -I/-İ/-U/-Ü аккузатив), "
            "посессивы (kalemim, kalemin), числа, время (saat), "
            "вопросы (ne / kim / nerede), demonstratives (bu/şu/o), "
            "var/yok, FSTKÇŞHP, гармония гласных."
        ),
        "num_questions": 22,
    },
    {
        "name": "Часть 2 · Продвинутая",
        "topics_hint": (
            "прошедшее -DI (+ нерегулярные), будущее -EcEK, modal istemek, "
            "именное сказуемое и değil, neden / için / -mak için, "
            "изафеты (annemin tişörtü, kahve makinesi, tahta masa), "
            "imperative gel/git, daha/en, çünkü/-diği için, -li/-lik/-siz, "
            "-deki, conjunctions (ve/ama/çünkü), -arak/-erek, -dir certainty, "
            "değil mi tag, -ken, -alım/-elim/-ayım/-eyim, "
            "relative clauses -EN/-DİĞİ, suffix -Kİ, наречия -CE/-CA."
        ),
        "num_questions": 22,
    },
]


def _build_part_prompt(part: dict, progress: dict, lesson_index: list[tuple[str, str]]) -> str:
    progress_json = json.dumps(progress, ensure_ascii=False, indent=2)
    index_lines = "\n".join(f"- {name} — {title}" for name, title in lesson_index)
    return f"""Сгенерируй **{part['name']}** диагностического мини-экзамена.

Это часть бóльшего экзамена, поэтому формат вывода — **только блок
вопросов**, без HTML-обвязки (страница соберётся отдельно).

## Контекст ученика

```json
{progress_json}
```

## Темы, которые надо покрыть в этой части

{part['topics_hint']}

## Доступные конспекты (только как референс — какие темы существуют)

{index_lines}

## Что выдать

Ровно **{part['num_questions']} вопросов** по темам выше — стараясь
покрыть как можно больше разных тем (≈2 вопроса на тему). Типы:

- **multiple choice** (4 варианта) — для распознавания формы;
- **fill-in** (текстовое поле + кнопка «Проверить») — для применения
  правил.

Не повторяй однотипные задачи подряд, чередуй. Используй уже знакомую
ученику лексику (она в `vocabulary_bank` выше).

## Формат вывода

**Только один блок** `<questions>...</questions>` со списком вопросов.
Никакого HTML-документа, никакого `<html>`, никакого CSS — только сами
карточки вопросов в едином стиле (использовать те же CSS-классы, что и
в обычных тренировках: `.exercise`, `.mc-options`, `.mc-btn`,
`.check-btn`, `.feedback`).

**Каждый вопрос** — `<div class="exercise" data-topic="ИДЕНТИФИКАТОР_ТЕМЫ">`,
где `ИДЕНТИФИКАТОР_ТЕМЫ` — короткая стабильная строка-ключ для
последующей сводки. Примеры идентификаторов:
- `"locative -de/-da"`
- `"dative -e/-a"`
- `"past -DI"`
- `"past -DI irregular"`
- `"future -EcEK"`
- `"definite compound"`
- `"indefinite compound"`
- `"possessive suffix"`
- `"-DİĞİ relative"`
- `"-EN/-AN relative"`
- `"-CE/-CA adverbs"`
- и т.п.

Используй один и тот же ключ для всех вопросов по одной теме, чтобы JS
мог сгруппировать. Темы выбирай из списка тем выше.

**Структура одного multiple choice**:
```html
<div class="exercise" data-topic="locative -de/-da">
  <div class="q">Где это? "На столе" по-турецки:</div>
  <div class="mc-options"
       data-correct="masada">
    <button class="mc-btn">masadan</button>
    <button class="mc-btn">masaya</button>
    <button class="mc-btn">masada</button>
    <button class="mc-btn">masayı</button>
  </div>
  <div class="feedback"></div>
  <div class="hint" style="display:none">
    -DA добавляется к слову с задней гласной (a/ı/o/u).
  </div>
</div>
```

**Структура fill-in**:
```html
<div class="exercise" data-topic="past -DI">
  <div class="q">Заполни пропуск: «Dün okula __» (я вчера пошёл в школу — глагол gitmek):</div>
  <input type="text"
         data-correct="gittim|Gittim"
         placeholder="…">
  <button class="check-btn">Проверить</button>
  <div class="feedback"></div>
  <div class="hint" style="display:none">
    -DI past: gid- + -di + -m, FSTKÇŞHP: t→d не нужно (-dim после d/m/n).
  </div>
</div>
```

В `data-correct` для fill-in можно перечислить несколько правильных
вариантов через `|` (разные написания, синонимы).

В `hint` — короткое объяснение (1–2 строки), почему такой ответ. JS
покажет это, когда ученик ответит.

## Контракт вывода

Сначала блок `<manifest>` (для совместимости с моим парсером):

```
<manifest>
{{"part": "{part['name']}", "num_questions": {part['num_questions']}}}
</manifest>
```

Затем `<questions>...</questions>` с {part['num_questions']} карточками.

Никакого текста до `<manifest>` и после `</questions>`."""


def _call_part(client: anthropic.Anthropic, system_blocks: list[dict], user_msg: str,
               part_name: str) -> str:
    """Вызвать API и вернуть содержимое <questions>."""
    chunks: list[str] = []
    with client.messages.stream(
        model=gen.MODEL,
        max_tokens=gen.MAX_TOKENS,
        system=system_blocks,
        messages=[{"role": "user", "content": user_msg}],
    ) as stream:
        for delta in stream.text_stream:
            chunks.append(delta)
    text = "".join(chunks)
    print(f"[anthropic] {part_name}: получено {len(text)} символов", flush=True)
    m = QUESTIONS_RE.search(text)
    if not m:
        raise ValueError(
            f"{part_name}: в ответе нет блока <questions>. Сниппет: {text[:200]!r}"
        )
    return m.group(1).strip()


def _build_full_html(parts_questions: list[tuple[str, str]], today: str) -> str:
    """Склеить части в один самодостаточный HTML с движком сводки."""
    sections = []
    for part_name, questions_html in parts_questions:
        sections.append(f"""
<section class="section">
  <h2>🧪 {part_name}</h2>
  {questions_html}
</section>
""")

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>📊 Диагностический экзамен · {today}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; background: #0f1117; color: #e8e8e8; min-height: 100vh; padding: 20px; }}
  .container {{ max-width: 820px; margin: 0 auto; }}
  header {{ text-align: center; padding: 30px 0 24px; border-bottom: 1px solid #2a2a3a; margin-bottom: 28px; }}
  header h1 {{ font-size: 1.9rem; color: #fff; margin-bottom: 6px; }}
  header p {{ color: #888; font-size: 0.95rem; }}
  .badge {{ display: inline-block; background: #16a085; color: white; padding: 4px 12px; border-radius: 20px; font-size: 0.85rem; margin-top: 10px; }}
  .section {{ background: #181a24; border-radius: 14px; padding: 26px; margin-bottom: 22px; border: 1px solid #2a2a3a; }}
  .section h2 {{ font-size: 1.25rem; color: #f39c12; margin-bottom: 18px; display: flex; align-items: center; gap: 8px; }}
  .exercise {{ background: #1a1c2a; border-radius: 10px; padding: 16px; margin: 12px 0; border: 1px solid #2a2d40; }}
  .exercise .q {{ font-size: 1rem; color: #e8e8e8; margin-bottom: 12px; line-height: 1.6; }}
  .exercise input[type="text"] {{ background: #0f1117; border: 2px solid #2a2a3a; border-radius: 8px; color: #fff; font-size: 1rem; padding: 8px 14px; width: 100%; max-width: 360px; outline: none; }}
  .exercise input[type="text"]:focus {{ border-color: #3498db; }}
  .exercise input[type="text"].correct {{ border-color: #2ecc71; background: #0e1f12; }}
  .exercise input[type="text"].wrong {{ border-color: #e74c3c; background: #1f0e0e; }}
  .check-btn {{ background: #3498db; color: white; border: none; border-radius: 8px; padding: 8px 20px; margin-top: 10px; cursor: pointer; font-size: 0.95rem; }}
  .check-btn:hover {{ background: #2980b9; }}
  .feedback {{ font-size: 0.9rem; margin-top: 8px; min-height: 22px; }}
  .feedback.ok {{ color: #2ecc71; }}
  .feedback.err {{ color: #e74c3c; }}
  .hint {{ font-size: 0.85rem; color: #f39c12; margin-top: 6px; line-height: 1.5; background: #1e1a14; padding: 8px 12px; border-radius: 6px; border-left: 3px solid #f39c12; }}
  .mc-options {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px; }}
  .mc-btn {{ background: #1e2030; color: #e8e8e8; border: 2px solid #2a2d40; border-radius: 8px; padding: 8px 18px; cursor: pointer; font-size: 0.95rem; }}
  .mc-btn:hover:not(:disabled) {{ border-color: #3498db; }}
  .mc-btn:disabled {{ cursor: default; opacity: 0.5; }}
  .mc-btn.correct-choice {{ background: #1a2a1e; border-color: #2ecc71; color: #2ecc71; opacity: 1; }}
  .mc-btn.wrong-choice {{ background: #2d1010; border-color: #e74c3c; color: #e74c3c; }}
  #summary {{ background: #181a24; border-radius: 14px; padding: 28px; margin-top: 28px; border: 2px solid #f39c12; display: none; }}
  #summary.shown {{ display: block; }}
  #summary h2 {{ color: #f39c12; margin-bottom: 16px; }}
  .topic-row {{ display: flex; justify-content: space-between; align-items: center; padding: 8px 12px; margin: 6px 0; border-radius: 8px; background: #1a1c2a; }}
  .topic-row .topic {{ font-size: 0.95rem; }}
  .topic-row .score {{ font-weight: 600; }}
  .topic-row.green {{ border-left: 4px solid #2ecc71; }}
  .topic-row.yellow {{ border-left: 4px solid #f39c12; }}
  .topic-row.red {{ border-left: 4px solid #e74c3c; }}
  .topic-row.green .score {{ color: #2ecc71; }}
  .topic-row.yellow .score {{ color: #f39c12; }}
  .topic-row.red .score {{ color: #e74c3c; }}
  #weak-cmd-box {{ background: #0f1117; border: 1px solid #2a2a3a; border-radius: 8px; padding: 14px; margin-top: 18px; }}
  #weak-cmd {{ display: block; font-family: 'Courier New', monospace; color: #2ecc71; word-break: break-all; margin-bottom: 10px; line-height: 1.5; }}
  .copy-btn {{ background: #f39c12; color: #181a24; border: none; border-radius: 6px; padding: 8px 16px; cursor: pointer; font-weight: 600; }}
  .copy-btn:hover {{ background: #e08e0b; }}
  .copy-btn.copied {{ background: #2ecc71; }}
  .progress {{ font-size: 0.9rem; color: #888; margin-top: 8px; }}
  .finish-btn {{ display: block; margin: 28px auto 0; background: #2ecc71; color: #0f1117; border: none; border-radius: 10px; padding: 14px 32px; font-size: 1.05rem; font-weight: 600; cursor: pointer; }}
  .finish-btn:hover {{ background: #27ae60; }}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>📊 Диагностический мини-экзамен</h1>
    <p>{today} · по пройденным темам Cowork-курса</p>
    <span class="badge">~44 вопроса · 2 части</span>
  </header>

  <div class="section">
    <p style="color:#bbb; line-height:1.6;">
      Цель — понять, где у тебя реальные провалы. Не страшно ошибиться:
      именно эти темы попадут в <code>weak_topics</code> и начнут
      приоритетно повторяться в Recall будущих тренировок.<br><br>
      Отвечай быстро и не подсматривай. Объяснение появится сразу после
      ответа. Когда закончишь — нажми «📊 Подсчитать сводку» внизу.
    </p>
    <p class="progress" id="progress">Готово: 0 из 0</p>
  </div>

  {''.join(sections)}

  <button class="finish-btn" onclick="renderSummary()">📊 Подсчитать сводку</button>

  <div id="summary">
    <h2>Сводка по темам</h2>
    <p style="color:#bbb;">🟢 ≥80% правильных · 🟡 50–79% · 🔴 &lt;50%</p>
    <div id="topic-list" style="margin-top:16px;"></div>

    <div id="weak-cmd-box">
      <p style="color:#888; margin-bottom:8px;">Скопируй и пришли мне (ассистенту) в чат — слабые темы попадут в weak_topics:</p>
      <code id="weak-cmd"></code>
      <button class="copy-btn" onclick="copyCmd()">📋 Скопировать</button>
    </div>
  </div>
</div>

<script>
(function() {{
  const exercises = document.querySelectorAll('.exercise');
  const state = new Map(); // exercise -> {{topic, correct?}}

  function norm(s) {{ return (s || '').trim().toLowerCase(); }}

  function recordResult(ex, ok) {{
    state.set(ex, {{ topic: ex.dataset.topic, ok }});
    updateProgress();
    const hint = ex.querySelector('.hint');
    if (hint) hint.style.display = 'block';
  }}

  function updateProgress() {{
    document.getElementById('progress').textContent =
      `Готово: ${{state.size}} из ${{exercises.length}}`;
  }}

  // multiple choice
  document.querySelectorAll('.mc-options').forEach(opts => {{
    const correct = norm(opts.dataset.correct);
    const ex = opts.closest('.exercise');
    const fb = ex.querySelector('.feedback');
    opts.querySelectorAll('.mc-btn').forEach(btn => {{
      btn.addEventListener('click', () => {{
        if (state.has(ex)) return;
        const chosen = norm(btn.textContent);
        const ok = chosen === correct;
        btn.classList.add(ok ? 'correct-choice' : 'wrong-choice');
        if (!ok) {{
          // подсветим правильный
          opts.querySelectorAll('.mc-btn').forEach(b => {{
            if (norm(b.textContent) === correct) b.classList.add('correct-choice');
          }});
        }}
        opts.querySelectorAll('.mc-btn').forEach(b => b.disabled = true);
        fb.className = 'feedback ' + (ok ? 'ok' : 'err');
        fb.textContent = ok ? '✓ Верно' : '✗ Правильный ответ выделен зелёным';
        recordResult(ex, ok);
      }});
    }});
  }});

  // fill-in
  document.querySelectorAll('.check-btn').forEach(btn => {{
    const ex = btn.closest('.exercise');
    const input = ex.querySelector('input[type="text"]');
    const fb = ex.querySelector('.feedback');
    if (!input) return;
    const corrects = (input.dataset.correct || '').split('|').map(norm);
    btn.addEventListener('click', () => {{
      if (state.has(ex)) return;
      const ans = norm(input.value);
      const ok = corrects.includes(ans);
      input.classList.add(ok ? 'correct' : 'wrong');
      input.disabled = true;
      btn.disabled = true;
      fb.className = 'feedback ' + (ok ? 'ok' : 'err');
      fb.textContent = ok ? '✓ Верно' : '✗ Правильный ответ: ' + corrects[0];
      recordResult(ex, ok);
    }});
  }});

  document.getElementById('progress').textContent = `Готово: 0 из ${{exercises.length}}`;

  window.renderSummary = function() {{
    const byTopic = new Map();
    for (const {{topic, ok}} of state.values()) {{
      if (!byTopic.has(topic)) byTopic.set(topic, {{ok:0, total:0}});
      const s = byTopic.get(topic);
      s.total++;
      if (ok) s.ok++;
    }}
    const rows = [...byTopic.entries()]
      .map(([t, s]) => ({{topic: t, score: s.ok/s.total, ok: s.ok, total: s.total}}))
      .sort((a, b) => a.score - b.score);

    const html = rows.map(r => {{
      const pct = Math.round(r.score * 100);
      const cls = r.score >= 0.8 ? 'green' : r.score >= 0.5 ? 'yellow' : 'red';
      const ico = cls === 'green' ? '🟢' : cls === 'yellow' ? '🟡' : '🔴';
      return `<div class="topic-row ${{cls}}"><span class="topic">${{ico}} ${{r.topic}}</span><span class="score">${{pct}}% (${{r.ok}}/${{r.total}})</span></div>`;
    }}).join('');
    document.getElementById('topic-list').innerHTML = html;

    const weak = rows.filter(r => r.score < 0.8).map(r => r.topic);
    const cmd = weak.length
      ? '/topics ' + weak.join(', ')
      : 'Все темы зелёные! 🎉  Скажи мне просто «всё ок».';
    document.getElementById('weak-cmd').textContent = cmd;

    const summary = document.getElementById('summary');
    summary.classList.add('shown');
    summary.scrollIntoView({{behavior:'smooth'}});
  }};

  window.copyCmd = function() {{
    const txt = document.getElementById('weak-cmd').textContent;
    navigator.clipboard.writeText(txt).then(() => {{
      const btn = document.querySelector('.copy-btn');
      btn.textContent = '✓ Скопировано';
      btn.classList.add('copied');
      setTimeout(() => {{ btn.textContent = '📋 Скопировать'; btn.classList.remove('copied'); }}, 2000);
    }});
  }};
}})();
</script>
</body>
</html>"""


def main() -> int:
    today = common.today_str()
    AUDITS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = AUDITS_DIR / f"exam_{today}.html"

    if out_path.exists():
        print(f"[exam] {out_path.name} уже существует, переотправляю.")
        common.send_document(out_path, caption="Диагностический экзамен (повторная отправка).")
        return 0

    tutor_prompt = gen._read(common.REPO_ROOT / "tutor_prompt.md")
    vocab_spec = gen._read(common.REPO_ROOT / "VOCAB_TRAINER_SPEC.md")
    rules = gen._read(common.REPO_ROOT / "generation_rules.md")
    progress = common.load_progress()
    index = gen._lesson_index()

    system_blocks = [
        {"type": "text", "text": tutor_prompt},
        {"type": "text", "text": vocab_spec + "\n\n" + rules},
    ]

    client = anthropic.Anthropic(timeout=1800.0)
    parts_questions: list[tuple[str, str]] = []
    for part in PARTS:
        user_msg = _build_part_prompt(part, progress, index)
        try:
            questions_html = _call_part(client, system_blocks, user_msg, part["name"])
        except (ValueError, Exception) as exc:
            common.send_message(
                f"Экзамен: ошибка генерации части «{part['name']}»: {exc}"
            )
            print(f"[error] {exc}", file=sys.stderr, flush=True)
            return 2
        parts_questions.append((part["name"], questions_html))

    html = _build_full_html(parts_questions, today)
    out_path.write_text(html, encoding="utf-8")

    common.send_document(
        out_path,
        caption=(
            "📊 Диагностический мини-экзамен (~44 вопроса, 2 части). "
            "Пройди, нажми «Подсчитать сводку», скопируй команду и "
            "пришли мне в чат — слабые темы попадут в weak_topics."
        ),
    )
    print(f"[ok] {out_path.name} сгенерирован и отправлен", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
