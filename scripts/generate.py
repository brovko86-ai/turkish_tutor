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

# Сдвиг к словарному запасу: 8 новых слов в день вместо 5 (с 2026-05-25,
# по запросу ученика — слабое место именно лексика).
NEW_WORDS_PER_DAY = 8

# Максимум сессий на одну тему. После — `is_new_lesson=true` обязательно.
# Лечит наблюдавшийся баг «модель крутится на одном уроке 3+ дня».
MAX_SESSIONS_PER_LESSON = 2

# Сколько ещё не пройденных глаголов из verbs_master_list передавать
# модели как «приоритетный пул для new_words».
VERBS_POOL_SIZE = 40

# Сколько weak_words модель обязана задействовать в упражнениях.
WEAK_WORDS_MIN_USAGE = 4

# Финальный урок курса Cowork. После него — режим expansion (повторение +
# темы вне исходных материалов). Пока режим обозначается, но переключение
# поведения будет в следующей итерации.
LAST_CURRICULUM_LESSON = 32

MANIFEST_RE = re.compile(r"<manifest>\s*(\{.*?\})\s*</manifest>", re.DOTALL)
HTML_RE = re.compile(r"<html\b.*?</html\s*>", re.DOTALL | re.IGNORECASE)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _style_sample() -> str:
    """Краткая выжимка стиля: только `<head>` и первый `<section>`
    последней тренировки. Этого хватает модели, чтобы понять CSS-каркас
    и палитру. Полный HTML раньше был 8000 симв (~2k tokens); сейчас
    ~2500 симв (~600 tokens) — экономия ~1.5k input tokens/вызов.
    """
    sample = common.latest_training()
    if sample is None:
        return "(образцов предыдущих тренировок ещё нет)"
    text = sample.read_text(encoding="utf-8")
    # берём до первого закрытия section (плюс запас) — там уже видно
    # заголовок, CSS-переменные, структуру блока.
    cut = text.find("</section>")
    if cut > 0:
        text = text[: cut + len("</section>")]
    return f"Файл: {sample.name}\n\n{text[:2500]}"


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


# verbs_master_list.md → четвёрки строк: No / TR / EN / object suffix.
# Глагол — латиница, обычно `*mek` или `*mak`, может быть составным
# (`ara vermek`, `alay etmek`).
_VERBS_FILE = common.REPO_ROOT / "lesson_materials" / "verbs_master_list.md"


# Расширенный fallback-пул: B2/C1 глаголы вне ТОП-200 Cowork-курса.
# Активируется когда verbs_master_list исчерпан (у ученика в long_term
# уже все ТОП-200 глаголов). Разные семантические поля: эмоции,
# общение, работа, наука, отношения, повседневность. Object suffix
# указан там, где принципиален.
_EXTRA_VERBS_B2: list[tuple[str, str, str]] = [
    # эмоции / внутренние состояния
    ("özlemek",       "to miss / to long for",              "-ı, -i"),
    ("pişman olmak",  "to regret",                          "-a, -e"),
    ("gurur duymak",  "to be proud of",                     "-la"),
    ("hayret etmek",  "to be amazed / astonished",          "-a, -e"),
    ("sıkılmak",      "to get bored",                       "-dan, -den"),
    ("rahatsız olmak","to feel uncomfortable",              "-dan, -den"),
    # коммуникация / убеждение
    ("ikna etmek",    "to persuade / convince",             "-ı, -i"),
    ("itiraz etmek",  "to object / protest",                "-a, -e"),
    ("belirtmek",     "to state / indicate",                "-ı, -i"),
    ("vurgulamak",    "to emphasise",                       "-ı, -i"),
    ("özetlemek",     "to summarise",                       "-ı, -i"),
    ("yorumlamak",    "to comment / interpret",             "-ı, -i"),
    # работа / принятие решений
    ("planlamak",     "to plan",                            "-ı, -i"),
    ("başvurmak",     "to apply for / consult",             "-a, -e"),
    ("üstlenmek",     "to take on / undertake",             "-ı, -i"),
    ("gerçekleştirmek","to realise / carry out",            "-ı, -i"),
    ("hedeflemek",    "to aim / target",                    "-ı, -i"),
    ("değerlendirmek","to evaluate / assess",               "-ı, -i"),
    # мышление / знание
    ("varsaymak",     "to assume",                          "-ı, -i"),
    ("çıkarmak",      "to infer / deduce",                  "-ı, -i"),
    ("sezmek",        "to sense / perceive",                "-ı, -i"),
    ("kavramak",      "to grasp / comprehend",              "-ı, -i"),
    ("keşfetmek",     "to discover",                        "-ı, -i"),
    # отношения / социальное
    ("tanıştırmak",   "to introduce (someone to)",          "-la"),
    ("davet etmek",   "to invite",                          "-ı, -i"),
    ("desteklemek",   "to support",                         "-ı, -i"),
    ("kıskanmak",     "to envy / be jealous of",            "-ı, -i"),
    ("affetmek",      "to forgive",                         "-ı, -i"),
    # действие / физика
    ("kaydetmek",     "to record / save",                   "-ı, -i"),
    ("silmek",        "to erase / wipe",                    "-ı, -i"),
    ("bağlamak",      "to tie / connect",                   "-a, -e"),
    ("çözmek",        "to solve / untie",                   "-ı, -i"),
    ("değiştirmek",   "to change",                          "-ı, -i"),
    # уровень C1 — редкие но полезные
    ("çekinmek",      "to hesitate / abstain",              "-dan, -den"),
    ("dayanmak",      "to endure / lean on",                "-a, -e"),
    ("yansımak",      "to be reflected",                    "-a, -e"),
    ("yer almak",     "to take place / be included",        "-da, -de"),
]


# Fallback-пул НЕ-глаголов: существительные, прилагательные, наречия
# B2/C1 уровня. Используется на финальной retry-попытке когда модель
# упорно повторяет known-лексику в «свободных» слотах new_words.
# Формат: (tr, en) — object suffix не нужен.
_EXTRA_NON_VERBS_B2: list[tuple[str, str]] = [
    # абстрактные существительные
    ("umut",           "hope"),
    ("kanaat",         "conviction / opinion"),
    ("özgürlük",       "freedom"),
    ("eşitlik",        "equality"),
    ("adalet",         "justice"),
    ("çaba",           "effort / attempt"),
    ("beceri",         "skill"),
    ("yetenek",        "ability / talent"),
    ("tecrübe",        "experience"),
    ("hayal",          "dream / imagination"),
    ("ihtiyaç",        "need"),
    ("ilgi",           "interest / attention"),
    # социальные / бытовые
    ("komşu",          "neighbour"),
    ("misafir",        "guest"),
    ("hediye",         "gift"),
    ("davetiye",       "invitation"),
    ("gelenek",        "tradition"),
    ("adet",           "custom"),
    # прилагательные — характер / состояние
    ("cömert",         "generous"),
    ("kibar",          "polite"),
    ("kaba",           "rude"),
    ("cesur",          "brave"),
    ("utangaç",        "shy"),
    ("dikkatsiz",      "careless"),
    ("bencil",         "selfish"),
    ("olgun",          "mature"),
    # прилагательные — качество
    ("verimli",        "productive / fertile"),
    ("gereksiz",       "unnecessary"),
    ("etkili",         "effective"),
    ("yararlı",        "useful"),
    ("zararlı",        "harmful"),
    # наречия / служебные
    ("aniden",         "suddenly"),
    ("kesinlikle değil","definitely not"),
    ("belki de",       "perhaps / maybe"),
    ("her halükarda",  "in any case"),
    ("özellikle",      "especially"),
    ("aslında",        "actually / in fact"),
]


def _non_verbs_pool(progress: dict, limit: int = 20) -> list[tuple[str, str]]:
    """Свежие существительные/прилагательные/наречия (не в known)."""
    known = _known_words(progress)
    return [(tr, en) for tr, en in _EXTRA_NON_VERBS_B2
            if tr.strip().lower() not in known][:limit]


def _parse_verbs_master_list() -> list[tuple[str, str, str]]:
    """Возвращает список (tr, en, object_suffix) из verbs_master_list.md.

    Жёстко привязан к формату, в котором был экспортирован Google Doc:
    каждая запись = 4 строки, начинающиеся с табуляции (No, tr, en, obj).
    Парсер тривиальный: ищем последовательные блоки.
    """
    if not _VERBS_FILE.exists():
        return []
    raw = _VERBS_FILE.read_text(encoding="utf-8")
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    out: list[tuple[str, str, str]] = []
    i = 0
    while i < len(lines):
        # ищем строку — целое число (номер записи)
        if lines[i].isdigit() and i + 3 < len(lines):
            tr = lines[i + 1]
            en = lines[i + 2]
            obj = lines[i + 3]
            # глагол валиден если латиница и оканчивается на mek/mak
            if re.search(r"m[ea]k$", tr) and not tr.startswith("-"):
                out.append((tr, en, obj))
            i += 4
        else:
            i += 1
    return out


def _verbs_pool(progress: dict, limit: int = VERBS_POOL_SIZE) -> list[tuple[str, str, str]]:
    """Топ-N глаголов, которые ученик ещё НЕ знает. Сначала берём из
    verbs_master_list (ТОП-200 Cowork), потом добираем из встроенного
    B2/C1 fallback-списка. Передаётся модели как приоритетный пул для
    new_words."""
    known = _known_words(progress)
    result: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for source in (_parse_verbs_master_list(), _EXTRA_VERBS_B2):
        for tr, en, obj in source:
            key = tr.strip().lower()
            if key in known or key in seen:
                continue
            seen.add(key)
            result.append((tr, en, obj))
            if len(result) >= limit:
                return result
    return result


def _compact_progress(progress: dict) -> dict:
    """Копия progress с обрезанным long_term (модель этот массив не
    использует, но он тянет ~14k input tokens). Оставляем только счётчик
    и последние 10 слов как sample. Всё остальное — как есть."""
    bank = progress.get("vocabulary_bank", {}) or {}
    long_term = bank.get("long_term", []) or []
    compact = dict(progress)
    compact_bank = dict(bank)
    compact_bank["long_term"] = f"(list of {len(long_term)} words, omitted for brevity)"
    compact["vocabulary_bank"] = compact_bank
    return compact


def _build_user_message(today: str, progress: dict, style: str,
                        lesson_material: str, material_note: str,
                        lesson_index: list[tuple[str, str]]) -> str:
    progress_json = json.dumps(_compact_progress(progress), ensure_ascii=False, indent=2)
    lesson_title = progress.get("lesson_title", "(не задано)")
    lesson_number = progress.get("current_lesson", "?")
    session_number = int(progress.get("session_number", 1) or 1)
    known = sorted(_known_words(progress))
    weak = progress.get("weak_words", []) or []
    weak_topics = progress.get("weak_topics", []) or []
    mode = progress.get("mode", "curriculum")
    # Готовый список 8 слов для new_words (см. forced_words_block ниже).
    forced_verbs = _verbs_pool(progress, limit=4)
    forced_non_verbs = _non_verbs_pool(progress, limit=NEW_WORDS_PER_DAY - 4)
    forced_ready = len(forced_verbs) >= 4 and \
                   len(forced_non_verbs) >= NEW_WORDS_PER_DAY - 4

    # Качество упражнений: рандомизация MC + правильные hint'ы +
    # корректные пары в matching. Это часто нарушается, поэтому повторяем
    # явно в каждом user_message.
    quality_block = (
        "\n**КОМПАКТНОСТЬ HTML:** не пиши HTML-комментарии "
        "`<!-- ... -->` внутри тренировки, hint делай **одной строкой** "
        "(без абзацев). Всё остальное — как раньше: полноценная вёрстка, "
        "8 grammar-упражнений, все `data-correct` на месте, полный CSS. "
        "**Формат важнее компактности.**\n"
        "\n**КАЧЕСТВО УПРАЖНЕНИЙ (часто нарушается, перечитай):**\n"
        "- В **multiple-choice**: правильный ответ **не должен быть на "
        "первом месте**. Распределяй позицию правильного варианта "
        "равномерно по 1–4. Плюс в конце `<body>` добавь JS-shuffle "
        "`.mc-options` (snippet в `generation_rules.md` §7a). **Все 4 "
        "варианта должны быть ТЕКСТУАЛЬНО УНИКАЛЬНЫ**, никаких двух "
        "одинаковых строк (в 2026-06-10 было `koşarak geldi` дважды, "
        "одна красная, одна зелёная — это бессмыслица). И ровно одна "
        "кнопка имеет `data-correct=\"true\"`.\n"
        "- **Подсказка (`hint`) НИКОГДА не содержит сам ответ или его "
        "части.** Только грамматическое правило или перевод нового "
        "слова. Если задание — вставить `gittim`, hint описывает "
        "правило (`-DI` past + личное окончание `-m`, корень `git`), "
        "но **не называет** `gittim` ни одной буквой.\n"
        "- **Правильные ответы в `checkInput(...)` и `data-correct=` "
        "не должны содержать опечаток.** Проверяй каждое слово "
        "побуквенно. В 2026-06-11 был `toplatttı` (3 t) вместо "
        "`toplattı` (2 t) — это критическая опечатка в самом эталоне. "
        "В турецком три одинаковые буквы подряд **невозможны**.\n"
        "- **Matching (Сопоставь пары)**: КРИТИЧЕСКИ важная зона. "
        "Сперва на черновике выпиши 12 СЕМАНТИЧЕСКИ правильных пар "
        "(tr ↔ ru) опираясь на `vocabulary_bank`. Только потом ставь "
        "`data-id` так, чтобы tr и его правильный ru имели один и тот "
        "же id. Перед выводом мысленно пройди по всем парам и сверь "
        "корректность. Не дублируй ru-карточки (никаких двух "
        "«тратить» — один из них почти наверняка не тот, что нужен). "
        "Прошлая тренировка 2026-06-10 содержала 6 ошибок из 12 пар: "
        "`önermek`↔«праздновать» и т.п. — это полностью обесценивает "
        "упражнение.\n"
    )

    # Жёсткое утверждение про источник темы — первая важная вещь, которую
    # видит модель после даты.
    topic_anchor = (
        f"\n**ТЕМА УРОКА — ЕДИНСТВЕННЫЙ ИСТОЧНИК ИСТИНЫ:** "
        f"`lesson_title = «{lesson_title}»` из JSON ниже. "
        f"Сегодня сгенерируй тренировку именно по этой теме. "
        f"Не подменяй её другой темой, даже если в конспектах курса под "
        f"тем же номером урока {lesson_number} лежит что-то иное.\n"
    )

    # Сдвиг урока: после MAX_SESSIONS_PER_LESSON сессий на одной теме
    # обязательно переключиться. Сейчас прошлая тренировка имела
    # `session_number` = N → следующая либо N+1 (если ≤ макс), либо
    # принудительно новая тема.
    if session_number >= MAX_SESSIONS_PER_LESSON:
        session_anchor = (
            f"\n**ОБЯЗАТЕЛЬНЫЙ ПЕРЕХОД К СЛЕДУЮЩЕМУ УРОКУ:** прошлая "
            f"тренировка была `session_number={session_number}` по теме "
            f"«{lesson_title}». Лимит — {MAX_SESSIONS_PER_LESSON} сессии "
            f"на тему. Поэтому **в манифесте** этой тренировки:\n"
            f"- `is_new_lesson: true`\n"
            f"- `lesson_number: {int(lesson_number) + 1}` (или другой "
            f"  следующий по плану)\n"
            f"- `lesson_title:` — новая тема\n"
            f"- `session_number: 1`\n"
            f"Если ты пытаешься сохранить старую тему — генерация будет "
            f"отвергнута. Двигаемся вперёд.\n"
        )
    else:
        session_anchor = (
            f"\nЭто `session_number={session_number + 1}` по теме "
            f"«{lesson_title}». Можно ещё одна сессия по той же теме "
            f"(`is_new_lesson: false`) ИЛИ сразу переход на следующий "
            f"урок (`is_new_lesson: true`), если тема исчерпана.\n"
        )

    # Если пулы наполнены — форсируем все 8 слов сразу. Модель не
    # выбирает лексику, только строит HTML вокруг готовых слов. Это
    # избавляет от 2-3 попыток из-за повторов (модель упорно повторяет
    # знакомую лексику в свободных слотах). Экономия: ~2x токенов в
    # среднем + предсказуемое качество.
    # Fallback: если пулы истощены — старое поведение (forbidden + свобода).
    forced_words_block = ""
    forbidden_block = ""
    if forced_ready:
        forced_lines = "\n".join(
            f"  {i+1}. tr=\"{tr}\", ru=\"...\" (по переводу: {en})"
            for i, (tr, en) in enumerate(
                [(v[0], v[1]) for v in forced_verbs] + list(forced_non_verbs)
            )
        )
        forced_words_block = (
            f"\n🚨 **ЗАДАННЫЙ СПИСОК {NEW_WORDS_PER_DAY} НОВЫХ СЛОВ — "
            f"НЕ ИЗМЕНЯТЬ:**\n"
            f"В `manifest.new_words` положи **РОВНО эти {NEW_WORDS_PER_DAY} "
            f"турецких слов в этом порядке**. `tr`-значение = точная "
            f"строка из списка (регистр не важен), `ru` — твой перевод "
            f"по английскому:\n\n{forced_lines}\n\n"
            f"⚠️ **Валидатор проверит:** каждый `manifest.new_words[i].tr` "
            f"должен быть в этом списке. Если модель поставит СВОЙ "
            f"глагол/существительное (даже связанный с темой урока) — "
            f"валидация упадёт, будет retry, при повторе — тренировка "
            f"НЕ сохранится, ученик получит уведомление об ошибке.\n\n"
            f"Даже если тема урока подсказывает конкретные слова "
            f"(например тема «пассив» и хочется подставить `seçilmek`, "
            f"`açıklanmak` и т.п.) — НЕ ДЕЛАЙ этого. Эти слова уже "
            f"знакомы ученику. Возьми ТОЛЬКО из списка выше.\n\n"
            f"Задача разбита на 2 шага:\n"
            f"1) Записать эти {NEW_WORDS_PER_DAY} слов в манифест с "
            f"русским переводом;\n"
            f"2) Собрать HTML вокруг них: использовать в «🆕 новые слова», "
            f"«🎯 Глагол дня» (один из 4 глаголов), «✍️ упражнения», "
            f"«🎨 свободная продукция», примеры в грамматике. "
            f"Применяй грамматику урока К ЭТИМ словам.\n"
        )
    elif known:
        forbidden_block = (
            f"\n**ЗАПРЕТ НА ПОВТОР ЛЕКСИКИ** (fallback — пулы пуст): "
            f"в `manifest.new_words` должно быть {NEW_WORDS_PER_DAY} "
            f"турецких слов, которых **нет** в списке уже знакомых ученику "
            f"слов ({len(known)} шт):\n"
            f"```\n{', '.join(known)}\n```\n"
            f"Любой повтор приведёт к отказу генерации.\n"
        )

    # Приоритет для Recall: слабые темы по результатам диагностического
    # экзамена. Если есть — первые 2 задачи блока Recall должны быть по
    # ним. Под капотом — обычный список строк-идентификаторов тем.
    weak_topics_block = ""
    if weak_topics:
        weak_topics_lines = "\n".join(f"- {t}" for t in weak_topics)
        weak_topics_block = (
            f"\n**ПРИОРИТЕТ ДЛЯ RECALL — слабые темы по экзамену "
            f"({len(weak_topics)} шт):**\n"
            f"{weak_topics_lines}\n\n"
            f"Это темы, в которых ученик ошибался на диагностическом "
            f"мини-экзамене. **Минимум 2 из задач блока «⚡ Recall» "
            f"должны быть по этим темам** — короткие задания "
            f"(distinguish form, choose suffix, translate phrase). "
            f"Ротируй темы между тренировками, не повторяй одну и ту же "
            f"тему две тренировки подряд.\n"
        )

    # Обязательная ротация weak_words.
    weak_block = ""
    if weak:
        weak_lines = "\n".join(
            f"- `{w['tr']}` — {w['ru']}  (промахов: {w.get('fails', '?')})"
            for w in weak
        )
        weak_block = (
            f"\n**ОБЯЗАТЕЛЬНАЯ РАБОТА С weak_words ({len(weak)} шт):**\n"
            f"{weak_lines}\n\n"
            f"Это слова, на которых ученик уже ошибался. "
            f"**Минимум {WEAK_WORDS_MIN_USAGE} из них должны быть "
            f"задействованы в упражнениях этой тренировки** — особенно "
            f"в блоке «🧠 Словарный тренажёр» (часть «вставь "
            f"пропущенное») и в свободной продукции.\n\n"
            f"**Каждое упражнение, где встречается weak_word, обязано "
            f"иметь атрибут `data-weak=\"<tr>\"`** на корневом "
            f"`<div class=\"exercise\">`. JS-движок тренажёра отслеживает, "
            f"какие weak_words ученик ответил правильно, и в самом конце "
            f"страницы (после «🧭 Мини-ревью») выводит блок "
            f"«✅ Закрыть выученные» с копируемой командой "
            f"`/learned <tr1> <tr2> ...` — ученик отправит её боту, "
            f"и слова автоматически уйдут из weak_words. Это и есть "
            f"механизм закрытия weak_words по факту успешного прохождения.\n"
        )

    # Режим работы. Curriculum — обычный курс. Expansion — после
    # последнего урока, упор на повторение и новые темы вне Cowork-материалов.
    mode_block = ""
    if mode == "expansion":
        mode_block = (
            f"\n**РЕЖИМ EXPANSION** (после завершения курса Cowork): "
            f"новые темы — твой выбор по уровню ученика (каузатив, пассив, "
            f"плюсквамперфект, косвенная речь, идиомы, частотные сложные "
            f"конструкции). Активное повторение тем из `completed_lessons` "
            f"через recall и упражнения. Удвоенный объём словарной "
            f"практики.\n"
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

    # lesson_index (~550 tokens) полезен только когда:
    # (a) mode=expansion — модель может брать темы для recall из старых
    #     конспектов;
    # (b) material_note ненулевой — тема не совпала с файлом, полезно
    #     показать что вообще есть.
    # В обычном curriculum-run модель не смотрит на индекс, экономим.
    index_block = ""
    if lesson_index and (mode == "expansion" or material_note):
        lines = "\n".join(f"- {name} — {title}" for name, title in lesson_index)
        index_block = (
            f"\nДоступные конспекты курса (для справки — можно мысленно "
            f"сослаться, если нужна база по более ранней теме):\n{lines}\n"
        )

    return f"""Сгенерируй тренировку на сегодня — {today}.
{forced_words_block}{quality_block}{topic_anchor}{session_anchor}{mode_block}{forbidden_block}{weak_block}{weak_topics_block}
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
   все 9 блоков из `generation_rules.md` — включая новый блок
   «🎯 Глагол дня»).

Никакого текста до `<manifest>` и после `</html>`. JSON-манифест должен
быть валиден и содержать ровно {NEW_WORDS_PER_DAY} новых слов."""


_MATCH_CARD_RE = re.compile(
    r'<div[^>]*class="[^"]*match-card[^"]*"[^>]*>([^<]+)</div>',
    re.IGNORECASE,
)
_ATTR_RE = re.compile(r'(\w[\w-]*)\s*=\s*"([^"]*)"')
_RU_TOKEN_RE = re.compile(r"[а-яёa-z]{4,}", re.IGNORECASE)


def _ru_tokens(text: str) -> set[str]:
    """Множество значимых русских (или английских) слов из перевода."""
    return {m.lower() for m in _RU_TOKEN_RE.findall(text or "")}


def _check_matching_pairs(html: str, progress: dict,
                          manifest: dict) -> list[str]:
    """Возвращает список ошибок в matching-блоке (Сопоставь пары).

    Парсит все `match-card` с `data-id` и `data-side`, группирует по
    id, для каждой пары (tr, ru) сверяет ru с известным переводом
    из vocabulary_bank / weak_words / new_words.
    Возвращает человекочитаемые описания несоответствий.
    """
    # достаём ту самую секцию с card'ами
    bank = progress.get("vocabulary_bank", {}) or {}
    known: dict[str, set[str]] = {}
    for bucket in (bank.get("active", []), bank.get("long_term", [])):
        for w in bucket or []:
            tr = (w.get("tr") or "").strip().lower()
            if tr:
                known.setdefault(tr, set()).update(_ru_tokens(w.get("ru", "")))
    for w in progress.get("weak_words", []) or []:
        tr = (w.get("tr") or "").strip().lower()
        if tr:
            known.setdefault(tr, set()).update(_ru_tokens(w.get("ru", "")))
    for w in manifest.get("new_words", []) or []:
        tr = (w.get("tr") or "").strip().lower()
        if tr:
            known.setdefault(tr, set()).update(_ru_tokens(w.get("ru", "")))

    # парсим карточки: для каждой собираем data-id, data-side, текст
    cards: dict[str, dict[str, str]] = {}  # id -> {"tr": text, "ru": text}
    for m in re.finditer(
        r'<div\s+([^>]*?class="[^"]*match-card[^"]*"[^>]*)>([^<]+)</div>',
        html, re.IGNORECASE,
    ):
        attrs = dict(_ATTR_RE.findall(m.group(1)))
        did = attrs.get("data-id")
        side = (attrs.get("data-side") or "").lower()
        text = m.group(2).strip()
        if did and side in ("tr", "ru"):
            cards.setdefault(did, {})[side] = text

    errors: list[str] = []
    seen_ru: dict[str, str] = {}  # нормализованный ru → id
    for did, pair in cards.items():
        tr = (pair.get("tr") or "").strip()
        ru = (pair.get("ru") or "").strip()
        if not tr or not ru:
            errors.append(f"data-id={did}: неполная пара (tr={tr!r}, ru={ru!r})")
            continue

        # дубликаты ru-карточек — индикатор путаницы
        ru_key = ru.lower()
        if ru_key in seen_ru and seen_ru[ru_key] != did:
            errors.append(
                f"data-id={did}: ru-карточка «{ru}» дублирует "
                f"data-id={seen_ru[ru_key]}"
            )
        seen_ru[ru_key] = did

        # сверка с словарём
        tr_lower = tr.lower()
        if tr_lower not in known:
            continue  # слова нет в нашем банке, не можем проверить
        expected = known[tr_lower]
        actual = _ru_tokens(ru)
        if expected and not (expected & actual):
            errors.append(
                f"data-id={did}: tr=«{tr}» ↔ ru=«{ru}» — перевод не "
                f"совпадает с банком (ожидаются токены: "
                f"{', '.join(sorted(expected))})"
            )
    return errors


# `checkInput('id', 'answer', 'feedback_id')` — fill-in упражнения.
# Иногда варианты через `|`. Допускаем '...' и "...".
_CHECK_INPUT_RE = re.compile(
    r"""checkInput\(\s*['"][^'"]*['"]\s*,\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)
# Турецкие буквы. Слова длиннее ≥3, иначе false positives (HTML, JS).
_TR_WORD_RE = re.compile(r"[a-zçğıöşüâîû]{3,}", re.IGNORECASE)
# Три одинаковые буквы подряд — в турецком практически невозможно
# (правильный `toplattı`, опечатка `toplatttı`).
_TRIPLE_LETTER_RE = re.compile(r"(.)\1\1", re.IGNORECASE)


def _check_correct_answers(html: str) -> list[str]:
    """Ищет в правильных ответах (`checkInput(...)`, `data-correct="..."`)
    слова с 3+ одинаковыми буквами подряд — это орфографическая опечатка
    модели, не реальное турецкое слово.
    """
    errors: list[str] = []
    candidates: list[str] = []
    # из checkInput
    for m in _CHECK_INPUT_RE.finditer(html):
        # ответ может быть `gittim|Gittim` — разбиваем
        candidates.extend(p.strip() for p in m.group(1).split("|") if p.strip())
    # из data-correct="..." (только не true/false)
    for m in re.finditer(r'data-correct="([^"]+)"', html):
        val = m.group(1).strip()
        if val.lower() in ("true", "false", "1", "0", "yes", "no"):
            continue
        candidates.extend(p.strip() for p in val.split("|") if p.strip())

    seen: set[str] = set()
    for ans in candidates:
        if ans in seen:
            continue
        seen.add(ans)
        for word in _TR_WORD_RE.findall(ans):
            tm = _TRIPLE_LETTER_RE.search(word)
            if tm:
                errors.append(
                    f"правильный ответ «{ans}» содержит слово «{word}» с "
                    f"3 одинаковыми буквами подряд («{tm.group(0)}») — "
                    f"это опечатка, в турецком таких слов нет"
                )
                break
    return errors


_MC_OPTIONS_RE = re.compile(
    r'<div[^>]*class="[^"]*mc-options[^"]*"[^>]*>(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
_MC_BTN_RE = re.compile(
    r'<button[^>]*class="[^"]*mc-btn[^"]*"([^>]*)>([^<]+)</button>',
    re.IGNORECASE,
)


def _check_mc_options(html: str) -> list[str]:
    """Проверяет каждый .mc-options блок:

    - тексты кнопок должны быть уникальны внутри блока;
    - ровно одна кнопка должна иметь data-correct="true" (или иной
      аналог: data-correct, без =true; либо отсутствие — тогда правильный
      ответ должен помечаться по data-correct внутри .mc-options).
    Возвращает список ошибок для retry.
    """
    errors: list[str] = []
    for i, m in enumerate(_MC_OPTIONS_RE.finditer(html), start=1):
        block = m.group(1)
        btns = _MC_BTN_RE.findall(block)
        if not btns:
            continue
        texts = [t.strip() for _, t in btns]

        # 1. дубли текста
        seen: dict[str, int] = {}
        for t in texts:
            seen[t] = seen.get(t, 0) + 1
        dups = [t for t, c in seen.items() if c > 1]
        if dups:
            errors.append(
                f"mc-options #{i}: дубли вариантов "
                + ", ".join(f"«{t}»×{seen[t]}" for t in dups)
                + f" (всего кнопок: {len(texts)})"
            )

        # 2. ровно один data-correct=true
        attrs_per_btn = [dict(_ATTR_RE.findall(a)) for a, _ in btns]
        correct_flags = [
            (a.get("data-correct") or "").strip().lower() in ("true", "1", "yes")
            for a in attrs_per_btn
        ]
        n_correct = sum(correct_flags)
        # альтернативный механизм: data-correct у самого .mc-options
        if n_correct == 0:
            # ищем data-correct=... в самом mc-options-теге
            opts_attrs = dict(_ATTR_RE.findall(
                html[m.start():m.start() + html[m.start():].find('>')]
            ))
            if not opts_attrs.get("data-correct"):
                errors.append(
                    f"mc-options #{i}: нет правильного ответа "
                    f"(ни одной data-correct=true и нет data-correct у блока)"
                )
        elif n_correct > 1:
            errors.append(
                f"mc-options #{i}: правильных ответов {n_correct}, должен быть 1"
            )
    return errors


_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_BLANK_LINES_RE = re.compile(r"\n\s*\n+")


def _minify_html(html: str) -> str:
    """Убирает HTML-комментарии и пустые строки. Ничего внутри
    `<script>` / `<pre>` / `<textarea>` не трогает, чтобы не сломать JS.
    Модель может забыть избавиться от них — минификация делает это
    гарантированно и снижает размер файла (лишние байты в Telegram)."""
    # разбиваем на сегменты, чтобы не резать <pre>/<script>/<textarea>
    parts: list[str] = []
    protected_re = re.compile(
        r"(<(script|pre|textarea)\b.*?</\2>)", re.DOTALL | re.IGNORECASE
    )
    last = 0
    for m in protected_re.finditer(html):
        # обычный HTML до защищённого участка
        chunk = html[last:m.start()]
        chunk = _HTML_COMMENT_RE.sub("", chunk)
        chunk = _BLANK_LINES_RE.sub("\n", chunk)
        parts.append(chunk)
        parts.append(m.group(0))  # защищённый участок как есть
        last = m.end()
    tail = html[last:]
    tail = _HTML_COMMENT_RE.sub("", tail)
    tail = _BLANK_LINES_RE.sub("\n", tail)
    parts.append(tail)
    return "".join(parts)


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

    # Авто-переключение в режим expansion после последнего урока курса.
    if int(manifest["lesson_number"]) > LAST_CURRICULUM_LESSON:
        progress["mode"] = "expansion"
    else:
        progress.setdefault("mode", "curriculum")

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

    # Раздутый промпт (verbs pool + weak_topics + материалы) может тянуть
    # ответ дольше дефолтного httpx-таймаута. Поднимаем до 30 минут.
    client = anthropic.Anthropic(timeout=1800.0)
    known = _known_words(progress)

    # Если пулы наполнены — модель ОБЯЗАНА взять эти 8 слов. Валидатор
    # ниже проверяет что new_words == forced_pairs (по tr, без учёта
    # регистра). Иначе ValueError и retry.
    _forced_verbs = _verbs_pool(progress, limit=4)
    _forced_non_verbs = _non_verbs_pool(progress, limit=NEW_WORDS_PER_DAY - 4)
    forced_ready = (len(_forced_verbs) >= 4 and
                    len(_forced_non_verbs) >= NEW_WORDS_PER_DAY - 4)
    forced_tr_ordered: list[str] = []
    if forced_ready:
        forced_tr_ordered = (
            [v[0].strip().lower() for v in _forced_verbs[:4]] +
            [nv[0].strip().lower() for nv in _forced_non_verbs[:NEW_WORDS_PER_DAY - 4]]
        )

    def _call(extra_user_msg: str = "") -> tuple[dict, str]:
        """Один вызов API + парсинг. Возвращает (manifest, html) или
        бросает ValueError при невалидной структуре / повторах.
        `extra_user_msg` идёт в НАЧАЛО (retry-инструкции важнее полного
        промпта — модель до конца user_message не всегда долистывает)."""
        msg = ((extra_user_msg + "\n\n---\n\n") if extra_user_msg else "") + user_message
        chunks: list[str] = []
        # cache_control на system-блоках: на retry в течение 5 минут
        # получаем cache hit → −90% стоимости на ~8.7k system-tokens.
        # Первый вызов дня платит cache write (+25%), но следующие retry
        # сильно дешевле.
        with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[
                {"type": "text", "text": tutor_prompt,
                 "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": vocab_spec + "\n\n" + rules,
                 "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": msg}],
        ) as stream:
            for delta in stream.text_stream:
                chunks.append(delta)
        text = "".join(chunks)
        print(f"[anthropic] получено {len(text)} символов")
        m, h = _parse_response(text)

        # Строгая валидация forced-list: если код задал 8 слов, модель
        # обязана их использовать точь-в-точь (по tr, без учёта регистра
        # и краевых пробелов). Отклонения — retry с явной пометкой.
        if forced_ready:
            got_tr = [w["tr"].strip().lower() for w in m["new_words"]]
            missing = [tr for tr in forced_tr_ordered if tr not in got_tr]
            extras  = [tr for tr in got_tr if tr not in forced_tr_ordered]
            if missing or extras:
                raise ValueError(
                    f"new_words не совпадает с forced-list. "
                    f"пропущены: {missing or '—'}; "
                    f"лишние (не из forced): {extras or '—'}. "
                    f"forced был: {forced_tr_ordered}"
                )
        else:
            # fallback: пулы истощены → проверка на повторы known-лексики
            reps = [
                w for w in m["new_words"]
                if w["tr"].strip().lower() in known
            ]
            if reps:
                raise ValueError(
                    "повтор знакомой лексики: "
                    + ", ".join(f"{w['tr']!r}" for w in reps)
                )
        # Валидация matching-пар: парсер ищет matching-карточки и сверяет
        # tr ↔ ru через vocabulary_bank. Любое несовпадение → retry.
        matching_errors = _check_matching_pairs(h, progress, m)
        if matching_errors:
            raise ValueError(
                f"в matching {len(matching_errors)} семантических ошибок:\n  "
                + "\n  ".join(matching_errors[:5])
                + (f"\n  …и ещё {len(matching_errors) - 5}"
                   if len(matching_errors) > 5 else "")
            )
        # Валидация multiple-choice: уникальность кнопок + ровно один
        # data-correct=true. Прошлая тренировка имела два «koşarak geldi»,
        # один false, второй true — ученик не мог их различить.
        mc_errors = _check_mc_options(h)
        if mc_errors:
            raise ValueError(
                f"в multiple-choice {len(mc_errors)} проблем:\n  "
                + "\n  ".join(mc_errors[:5])
                + (f"\n  …и ещё {len(mc_errors) - 5}"
                   if len(mc_errors) > 5 else "")
            )
        # Валидация правильных ответов: опечатки с XXX (3 одинаковые
        # буквы подряд) — в 2026-06-11 был toplatttı (3t) вместо toplattı.
        spelling_errors = _check_correct_answers(h)
        if spelling_errors:
            raise ValueError(
                f"в правильных ответах {len(spelling_errors)} опечаток:\n  "
                + "\n  ".join(spelling_errors[:5])
                + (f"\n  …и ещё {len(spelling_errors) - 5}"
                   if len(spelling_errors) > 5 else "")
            )
        # Защита от «застрял на одной теме»: если прошлая сессия уже была
        # MAX_SESSIONS_PER_LESSON, новая обязана быть другой темой.
        prev_session = int(progress.get("session_number", 1) or 1)
        if prev_session >= MAX_SESSIONS_PER_LESSON and not m.get("is_new_lesson"):
            raise ValueError(
                f"тема не сдвинулась: прошлая session={prev_session}, "
                f"а в манифесте is_new_lesson=false. "
                f"Должно быть is_new_lesson=true (лимит "
                f"{MAX_SESSIONS_PER_LESSON} сессий на тему)."
            )
        return m, h

    # До 2 попыток. Список 8 слов уже форсирован в user_message
    # (forced_words_block), поэтому эскалация нужна только на
    # содержательные ошибки: MC-дубли, matching-ошибки, опечатки, session-lock,
    # сеть. Повторов лексики быть не должно — но fallback остаётся.
    manifest: dict | None = None
    html: str = ""
    last_error: str = ""
    for attempt in (1, 2):
        extra = ""
        if attempt == 2:
            extra = (
                f"⚠️ Предыдущая попытка отклонена: {last_error}\n\n"
                f"Сгенерируй заново, целиком. Соблюдай ВСЕ требования из "
                f"user_message ниже — особенно ЗАДАННЫЙ СПИСОК 8 слов "
                f"(если он есть), уникальность MC-вариантов, семантику "
                f"matching-пар, отсутствие опечаток в правильных ответах."
            )
        try:
            manifest, html = _call(extra)
            break
        except ValueError as exc:
            last_error = str(exc)
            print(f"[retry] попытка {attempt} провалена: {exc}", file=sys.stderr)
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            # Сетевые сбои Anthropic: peer closed / read timeout / DNS.
            # Не наша логика — просто ретраим.
            last_error = f"сеть/Anthropic: {exc}"
            print(f"[retry] попытка {attempt} сетевой сбой: {exc}",
                  file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            # http-уровневые исключения (httpx.RemoteProtocolError,
            # httpx.ReadTimeout и т.п.) — SDK иногда не оборачивает.
            # Ретраим только известные сетевые классы.
            name = type(exc).__name__
            if any(x in name for x in ("Timeout", "Connection", "Protocol",
                                        "Read", "Network")):
                last_error = f"сеть ({name}): {exc}"
                print(f"[retry] попытка {attempt} сеть ({name}): {exc}",
                      file=sys.stderr)
            else:
                raise
    if manifest is None:
        common.send_message(
            f"Тренировка {today}: после 2 попыток модель так и не дала "
            f"валидный ответ. Последняя ошибка: {last_error}"
        )
        print(f"[error] обе попытки провалены: {last_error}", file=sys.stderr)
        return 2

    common.TRAININGS_DIR.mkdir(parents=True, exist_ok=True)
    minified = _minify_html(html)
    saved_bytes = len(html) - len(minified)
    if saved_bytes > 0:
        print(f"[minify] сэкономлено {saved_bytes} байт "
              f"({len(html)} → {len(minified)})")
    out_path.write_text(minified + "\n", encoding="utf-8")

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
