# Спецификация блока «Словарный тренажёр»

Этот файл — обязательный гайд для Claude при генерации ежедневной тренировки. Каждый сгенерированный `training_*.html` ОБЯЗАН включать секцию «🧠 Словарный тренажёр» сразу ПОСЛЕ блока «📚 Словарная разминка» и ДО блока «⚡ Recall» по грамматике.

## Цель

Закрепление слов в долгосрочной памяти через множественный контакт в разных форматах. Заменяет старый «кликни на карточку» — он даёт только пассивное узнавание и не стимулирует force-recall.

## Параметры

- **Новые слова в день:** 5 (не 10!) — критично для retention
- **Слов в тренажёре:** ~18–22
- **Длительность:** 8–12 минут активной работы
- **Порядок секций в тренировке:**
  1. Словарная разминка (вчерашние слова, кликабельные карточки)
  2. **Сегодняшние 5 новых слов** (короткое введение с примерами)
  3. **🧠 Словарный тренажёр** ← НОВЫЕ ужé введены, можно сразу драйвить
  4. Recall (грамматика вчерашнего)
  5. Новая грамматика, упражнения, продакшн, мини-ревью

## Алгоритм отбора слов из lesson_progress.json

```
WEAK         = первые 6 из weak_words (или меньше, если их < 6)  # ПРИОРИТЕТ
NEW          = vocabulary_bank.active                     # 5 слов (текущий урок)
RECENT       = последние 5 из long_term                   # вчера
MEDIUM       = слова из long_term, чей "moved" 2–4 дня назад → выбрать 3
WEEKLY       = слова из long_term, чей "moved" 5–10 дней назад → выбрать 2
DEEP_REVIEW  = рандомные 2 из всего остального long_term

TOTAL        = WEAK + NEW + RECENT + MEDIUM + WEEKLY + DEEP_REVIEW = ~17–22 слов
```

Если в каком-то ведре не хватает — берём из соседнего (старшего) ведра.

**ВАЖНО:** `weak_words` всегда идут первыми и обязательно попадают в Free typing (часть 5) — самый сложный тип. Цель — вытащить эти слова в долгую память через продуктивный recall.

**Удаление из weak_words:** делается человеком вручную через `/weakclear` или автоматически при апгрейде (см. TODO ниже). Пока слово остаётся в списке, пока пользователь его не уберёт.

## Распределение слов по упражнениям

| Упражнение | Сколько вопросов | Какие слова |
|---|---|---|
| A. Recognition (TR→RU) | 6 | NEW + RECENT (по 3) |
| B. Production MC (RU→TR) | 5 | NEW + RECENT + MEDIUM |
| C. Matching pairs | 6 пар | MEDIUM + WEEKLY + DEEP_REVIEW |
| D. Fill-in-the-blank | 3 | NEW (приоритет) — слова в контексте предложения |
| E. Free typing (RU→TR) | 3 | NEW (2) + RECENT (1) — самое сложное |

Каждое слово появляется в 1–2 упражнениях максимум, чтобы было разнообразие.

## Distractor-стратегия

Неправильные варианты MC должны быть:
1. Из ТОЙ ЖЕ семантической области (если правильный ответ — «новость», distractors — другие абстрактные слова: вопрос, ответ, решение). НЕ «бежать» рядом с «новость».
2. Из long_term (чтобы заодно проверить и старые слова на узнавание).
3. Грамматически совместимы (одна часть речи, тот же падеж).

## HTML/JS структура — копируй и адаптируй

Стилевые классы те же, что и в остальной тренировке (`.section`, `.exercise`, `.mc-btn` и т.д.). Добавь специфические классы тренажёра.

### CSS additions (включить в `<style>`)

```css
.vocab-trainer { background: linear-gradient(135deg, #1a1d2e 0%, #181a24 100%); }
.trainer-progress { font-size: 0.95rem; color: #bbb; text-align: right; margin-bottom: 10px; }
.trainer-progress .score { color: #2ecc71; font-weight: 700; font-size: 1.1rem; }

.match-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 14px 0; }
.match-card { background: #1e2030; border: 2px solid #2a2d40; border-radius: 8px; padding: 10px 14px; cursor: pointer; color: #e8e8e8; font-size: 0.98rem; transition: all 0.2s; text-align: center; }
.match-card.selected { border-color: #f39c12; background: #2a2415; }
.match-card.matched-ok { border-color: #2ecc71; background: #1a2a1e; cursor: default; opacity: 0.5; pointer-events: none; }
.match-card.matched-fail { border-color: #e74c3c; background: #2d1010; animation: shake 0.4s; }
@keyframes shake { 0%,100%{transform:translateX(0)} 25%{transform:translateX(-6px)} 75%{transform:translateX(6px)} }

.trainer-summary { background: #1e2030; border-radius: 10px; padding: 16px 20px; margin-top: 18px; line-height: 1.7; display: none; }
.trainer-summary.shown { display: block; }
.trainer-summary .verdict { font-size: 1.1rem; font-weight: 600; }
.weak-list { color: #f39c12; font-style: italic; }
```

### Скелет блока

```html
<section class="section vocab-trainer">
  <h2>🧠 Словарный тренажёр</h2>
  <p style="color:#bbb; line-height:1.7; margin-bottom:14px;">
    Пять упражнений на 8–12 минут — главный двигатель долгой памяти. 
    Отвечай не глядя в подсказки. Если ошибся — это нормально, ошибки 
    закрепляют память сильнее, чем правильные ответы.
  </p>
  <div class="trainer-progress">
    Счёт: <span class="score" id="t-score">0</span> / <span id="t-total">23</span>
  </div>

  <!-- A. Распознавание -->
  <h3>Часть 1 · Распознавание (TR → RU)</h3>
  <!-- 6 exercise-блоков с .mc-options и onClick="trainerMC(this, true/false, 'tr_word')" -->

  <!-- B. Воспроизведение -->
  <h3>Часть 2 · Воспроизведение (RU → TR)</h3>
  <!-- 5 exercise-блоков -->

  <!-- C. Матчинг -->
  <h3>Часть 3 · Сопоставь пары</h3>
  <p style="color:#888; font-size:0.9rem;">Кликай по парам: сначала на турецкое слово, потом на правильный перевод.</p>
  <div class="match-grid" id="match-grid">
    <!-- 12 .match-card: 6 TR + 6 RU, перемешанных, у каждой data-id и data-side -->
  </div>

  <!-- D. Пропуски -->
  <h3>Часть 4 · Вставь слово в пропуск</h3>
  <!-- 3 exercise с input + check, проверяющим введённое слово (см. функцию trainerType) -->

  <!-- E. Свободный ввод -->
  <h3>Часть 5 · Переведи с русского на турецкий</h3>
  <!-- 3 exercise с input + check -->

  <div class="trainer-summary" id="t-summary"></div>
</section>
```

### JS добавки в `<script>`

```javascript
const trainerState = { correct: 0, total: 0, weak: [], totalQuestions: 23 };

function trainerMC(btn, isCorrect, word) {
  // Дизейблим все кнопки этого вопроса
  btn.parentElement.querySelectorAll('.mc-btn').forEach(b => { b.disabled = true; b.style.opacity = '0.55'; });
  btn.style.opacity = '1';
  if (isCorrect) {
    btn.classList.add('correct-choice');
    trainerState.correct++;
  } else {
    btn.classList.add('wrong-choice');
    trainerState.weak.push(word);
    // Покажем правильный ответ
    btn.parentElement.querySelectorAll('.mc-btn[data-correct="true"]').forEach(b => b.classList.add('correct-choice'));
  }
  trainerState.total++;
  updateTrainerScore();
}

function trainerType(inputId, correct, word) {
  const el = document.getElementById(inputId);
  const fb = document.getElementById(inputId + '-fb');
  const val = el.value.trim().toLowerCase();
  const ok = val === correct.toLowerCase();
  if (ok) {
    el.classList.add('correct');
    fb.className = 'feedback ok'; fb.textContent = '✓ Верно';
    trainerState.correct++;
  } else {
    el.classList.add('wrong');
    fb.className = 'feedback err'; fb.textContent = '✗ Правильно: ' + correct;
    trainerState.weak.push(word);
  }
  el.disabled = true;
  trainerState.total++;
  updateTrainerScore();
}

let matchSel = null;
function matchSelect(card) {
  if (card.classList.contains('matched-ok')) return;
  if (!matchSel) { matchSel = card; card.classList.add('selected'); return; }
  if (matchSel === card) { card.classList.remove('selected'); matchSel = null; return; }
  if (matchSel.dataset.id === card.dataset.id && matchSel.dataset.side !== card.dataset.side) {
    matchSel.classList.remove('selected'); matchSel.classList.add('matched-ok');
    card.classList.add('matched-ok');
    trainerState.correct++;
    matchSel = null;
  } else {
    const wrong1 = matchSel, wrong2 = card;
    wrong1.classList.add('matched-fail'); wrong2.classList.add('matched-fail');
    setTimeout(() => { wrong1.classList.remove('matched-fail','selected'); wrong2.classList.remove('matched-fail'); }, 500);
    trainerState.weak.push(matchSel.textContent);
    matchSel = null;
  }
  trainerState.total++;
  updateTrainerScore();
}

function updateTrainerScore() {
  document.getElementById('t-score').textContent = trainerState.correct;
  if (trainerState.total >= trainerState.totalQuestions) showTrainerSummary();
}

function showTrainerSummary() {
  const pct = Math.round(100 * trainerState.correct / trainerState.totalQuestions);
  let verdict;
  if (pct >= 90) verdict = '🏆 Великолепно!';
  else if (pct >= 70) verdict = '✅ Хорошо.';
  else if (pct >= 50) verdict = '⚡ Норм, но нужно ещё прокрутить.';
  else verdict = '🔄 Вернёмся к этим словам в следующих тренировках.';
  const weak = [...new Set(trainerState.weak)].slice(0,8);
  const weakHTML = weak.length ? `<div class="weak-list">Слабые места: ${weak.join(', ')}</div>` : '';
  const sum = document.getElementById('t-summary');
  sum.classList.add('shown');
  sum.innerHTML = `<div class="verdict">${verdict} ${trainerState.correct}/${trainerState.totalQuestions} (${pct}%)</div>${weakHTML}`;
}
```

## Что НЕ делать

- Не повторять одно и то же слово >2 раз в одном тренажёре
- Не делать distractors из совершенно несвязных тем (это упрощает угадывание)
- Не превышать 25 вопросов суммарно (утомление, теряется фокус)
- Не убирать ни одно из 5 типов упражнений (комплект отрабатывает разные виды памяти)

## Что делать в HTML-summary тренажёра

После прохождения всех упражнений в тренажёре HTML показывает финальный итог. ОБЯЗАТЕЛЬНО включи кнопку «📤 Отметить как слабые в боте» — она ведёт на:

```
https://t.me/myturkish_tutor_bot?text=%2Fweak+слово1+слово2+слово3
```

(Слова из массива `trainerState.weak`, URL-encoded.) При клике на телефоне откроется чат с ботом, где сообщение `/weak ...` уже подставлено — пользователю остаётся нажать «Отправить».

JS код для генерации этой ссылки уже включён в шаблон выше (`showTrainerSummary`).

## Реализованные фичи

- ✅ Команда `/drill` в боте — standalone сессия только по словарю, генерируется Python-скриптом (`drill_generator.py`), без LLM
- ✅ Команды `/weak`, `/weaklist`, `/weakclear` — управление списком слабых слов
- ✅ Кнопка «📤 Отметить как слабые» в HTML-summary
- ✅ `weak_words` приоритетно подаются в каждый тренажёр

## TODO для будущих итераций

- Автоматическое удаление слова из `weak_words` после 2 правильных ответов подряд (нужен механизм обратной связи из HTML в `lesson_progress.json` — либо через ту же кнопку «отметить как пройденные», либо через `/master слово`)
- Аудио к словам (turcкая озвучка для тренировки на слух)
- Sub-команды `/drill 30` — drill длительностью 30 вопросов (longer session)
