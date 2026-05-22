# lesson_materials/

Конспекты уроков из Cowork-папки
[Drive · Turkche](https://drive.google.com/drive/folders/1vEPLJBKT68HC0g_83Zf_Hi6JZD3nALCW)
— скачано через `export?format=txt`, чистый текст с минимальной разметкой.

## Как используется

`generate.py` при генерации тренировки:

1. **Конспект по номеру урока.** Берёт `current_lesson` из
   `lesson_progress.json` и ищет файл `lesson_NN.md`. Если найден —
   передаёт модели весь его текст.
2. **Эвристика темы.** Сверяет ключевые слова из `lesson_title` с
   текстом файла. Если совпадений нет — добавляет предупреждение, что
   номер совпал, но тема — нет (см. ниже про расхождение нумерации).
3. **Индекс всех файлов.** Всегда передаёт список «имя — заголовок» по
   всем доступным конспектам, чтобы модель могла мысленно опереться на
   более раннюю тему, даже если её текущий урок не покрыт.

## Расхождение нумерации (важно)

`lesson_NN.md` нумерован по **Cowork-курсу** (как было в Drive). А
`lesson_progress.json` ведёт нумерацию **реального прогресса ученика** —
курс с момента создания материалов был расширен. Прямо сейчас:

- `current_lesson = 31`, `lesson_title = "Суффикс -CE/-CA"` (наречия,
  языки, мнение).
- А `lesson_31.md` — это про **-ALIM/-ELİM** (suggestive mood).

При таком mismatch `generate.py` корректно предупреждает модель, и она
будет опираться на собственное знание грамматики, а не слепо копировать
конспект. Поэтому **переименовывать файлы под текущий прогресс не
надо** — они служат как библиотека базовой грамматики (-DI, изафет,
падежи и т.д.), которая всё равно используется в спиральных
повторениях.

## Текущее содержимое

**31 конспект** покрывает основу курса. Скачивание выполнено
автоматически из Google Docs `export?format=txt`. Заголовок
`# Lesson NN — TITLE` добавлен скриптом, основной текст — без правок.

| # | Файл | Тема |
|---|---|---|
| 1 | `lesson_01.md` | TÜRKÇEYE GİRİŞ — Turkish alphabet & introduction |
| 2 | `lesson_02.md` | BEN, SEN, O, BİZ, SİZ, ONLAR — Personal pronouns & vowel harmony |
| 3 | `lesson_03.md` | FıSTıKÇı ŞaHaP — Hard consonants, -d→-t mutation |
| 4 | — | *(отсутствует в Drive)* |
| 5 | `lesson_05.md` | VAR, YOK, VAR MI / -DE, -DA — Existence, locative suffix |
| 6 | `lesson_06.md` | BU, BURADA, BURASI / NE, KİM — Demonstratives & question words |
| 7 | `lesson_07.md` | SAYILAR — Numbers, kaç, ne kadar |
| 8 | `lesson_08.md` | GİDİYOR, GELİYOR — Present continuous (-YOR) |
| 9 | `lesson_09.md` | NE ZAMAN, SAAT KAÇTA — Telling time, days, months |
| 10 | `lesson_10.md` | -E, -A NEREYE — Dative case |
| 11 | `lesson_11.md` | -DEN, -DAN NEREDEN — Ablative case |
| 12 | `lesson_12.md` | -LE, -LA KİMİNLE — Instrumental case |
| 13 | `lesson_13.md` | -I, -İ, -U, -Ü NEYİ — Accusative case |
| 14 | `lesson_14.md` | KALEMİM, KALEMİN — Possessive suffixes |
| 15 | `lesson_15.md` | GİTTİ, GELDİ (-DI past) — Definite past tense |
| 16 | `lesson_16.md` | ÖNCE-SONRA — Before/after with nouns & verbs |
| 17 | `lesson_17.md` | NEDEN, İÇİN, -MAK İÇİN — Reasons & purpose |
| 18 | `lesson_18.md` | ANNEMİN TİŞÖRTÜ — Definite compound noun |
| 19 | `lesson_19.md` | KAHVE MAKİNESİ — Indefinite compound noun |
| 20 | `lesson_20.md` | TAHTA MASA — Compound noun with no endings |
| 21 | `lesson_21.md` | GİT, GEL, OTUR — EMİR KİPİ (imperative) |
| 22 | `lesson_22.md` | DAHA…, EN… — Comparative & superlative |
| 23 | `lesson_23.md` | ÇÜNKÜ, -DİĞİ İÇİN — Expressing reason |
| 24 | `lesson_24.md` | -Lİ, -LİK, -SİZ — Derivational suffixes |
| 25 | `lesson_25.md` | -DEKİ — "the one in/on/at" |
| 26 | `lesson_26.md` | VE, ÇÜNKÜ, AMA — Conjunctions |
| 27 | `lesson_27.md` | -ARAK, -EREK — "by doing / while doing" |
| 28 | `lesson_28.md` | -DIR, -DİR, -DUR, -DÜR — Certainty suffix |
| 29 | `lesson_29.md` | DEĞİL Mİ? — Tag questions |
| 30 | — | *(файл в Drive был пустой, 3 байта)* |
| 31 | `lesson_31.md` | GİDELİM, GELELİM / -ALIM, -ELİM — Suggestive mood (1pl) |
| 32 | `lesson_32.md` | GİDEYİM, GELEYİM / -AYIM, -EYİM — Suggestive mood (1sg) |
| — | `verbs_master_list.md` | Полный список турецких глаголов с переводами |

## Чего не хватает

- **Урок 4** (Nationality + `değil` + question `mı/mi/mu/mü`) — не было
  в Drive-папке.
- **Урок 30 (-KEN)** — файл в Drive был пустой (3 байта). Если тема
  важна — добавь руками `lesson_30.md` или попроси автора курса.
- **Уроки 33–36** (relative clauses `-DİĞİ`, adverb `-CE`, suffix
  `-Kİ`, complex constructions) — курс в Drive заканчивается на уроке
  32. Это именно та зона, в которой ученик находится сейчас. Можно
  попросить автора оригинального курса докинуть эти материалы, либо
  оставить как есть — модель опирается на собственное знание.

## Как пополнять / обновлять

- **Добавить новый урок:** положи файл `lesson_NN.md` (или `.txt`),
  начни с шапки `# Lesson NN — TITLE`. Скрипт подтянет автоматически.
- **Обновить существующий:** перекачай Google Doc через
  `curl -sL "https://docs.google.com/document/d/<ID>/export?format=txt" -o lesson_NN.md`
  и допиши шапку, либо отредактируй текст вручную.
- Файлы можно коммитить в репозиторий (репо публичный, материалы
  не приватные).
