# turkish-trainer

Ежедневная генерация турецких тренировок и команда `/today` для Telegram —
полностью на GitHub Actions, без локального демона. Архитектурный
референс — [`github_actions_plan.md`](../github_actions_plan.md) (вне этого
репо).

## Как это работает

- **`daily.yml`** — `cron 0 3 * * *` (06:00 MSK). Генерирует
  `trainings/training_YYYY-MM-DD.html`, обновляет `lesson_progress.json` и
  отправляет файл в Telegram.
- **`on_demand.yml`** — `cron */5 * * * *`. Опрашивает Telegram через
  `getUpdates`. Если найдена команда `/today` — запускает ту же генерацию.
  Задержка: 5–10 минут.

Оба workflow в одной concurrency-group `turkish-generation`, так что
параллельной генерации не будет.

## Первичная настройка

1. **Сделать репозиторий публичным** (для бесплатных минут Actions).
2. В *Settings → Secrets and variables → Actions* добавить:
   - `ANTHROPIC_API_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
3. Одноразово отключить webhook:
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/deleteWebhook"
   ```
4. Залить `tutor_prompt.md` — текст проектной инструкции Cowork (см.
   TODO-блок в файле).
5. Убедиться, что `lesson_progress.json`, `VOCAB_TRAINER_SPEC.md`,
   `generation_rules.md` и хотя бы одна тренировка-образец лежат в репо.
6. *Actions → daily-training → Run workflow* — проверить плановый путь.
7. Отправить боту `/today` — проверить on-demand путь.

## Локальный тест

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export ANTHROPIC_API_KEY=sk-ant-...
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
export RUN_MODE=on_demand        # или scheduled
python scripts/generate.py
```

Проверить: появился HTML в `trainings/`, обновился `lesson_progress.json`,
пришёл файл в Telegram.

## Структура

```
.github/workflows/
  daily.yml              ежедневный cron
  on_demand.yml          опрос /today каждые 5 минут
scripts/
  common.py              Telegram-API + загрузка/сохранение состояния
  generate.py            одна полная генерация тренировки
  poll_telegram.py       проверка /today и запуск генерации
trainings/               training_YYYY-MM-DD.html
state/
  telegram_offset.txt    последний обработанный update_id
tutor_prompt.md          system-prompt тьютора (из Cowork)
VOCAB_TRAINER_SPEC.md    спецификация словарного тренажёра
generation_rules.md      контракт вывода + 8 блоков тренировки
lesson_progress.json     текущий урок, активный/долгосрочный словарь
```

## Защита от гонок и дублей

- `concurrency.group: turkish-generation` (см. workflow) — GitHub строит
  запуски в очередь, никаких параллельных генераций.
- `generate.py` проверяет наличие `trainings/training_$TODAY.html`:
  - `scheduled` — выходит молча,
  - `on_demand` — пере-отправляет существующий файл.
- При ошибке валидации ответа модели `lesson_progress.json` **не
  меняется**, в Telegram уходит сообщение об ошибке, скрипт падает с
  ненулевым кодом — пропуск дня будет заметен.

## Замечания

- Часовой пояс в скрипте — MSK (UTC+3). Меняется в `common.py` (константа
  `LOCAL_TZ`) и в cron `daily.yml`.
- Модель — `claude-sonnet-4-6` по умолчанию, переопределяется через
  переменную окружения `ANTHROPIC_MODEL`.
- Старый `bot_listener.py` и Cowork-задачу `new_lesson` после перехода
  **обязательно остановить** — иначе апдейты Telegram будут расходиться
  между двумя получателями.
