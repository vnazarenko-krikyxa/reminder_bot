# Як запустити свого бота

Кожен запускає **власну копію**: свій Telegram-бот, свій ключ LLM (Groq, OpenAI, Claude, Gemini…) і (за бажанням) свій Google-акаунт.
Базові нагадування працюють без Google.

## 1. Що потрібно
- Python 3.11+
- Telegram-бот: у [@BotFather](https://t.me/BotFather) → `/newbot` → скопіюй токен.
- Свій Telegram id: напиши [@userinfobot](https://t.me/userinfobot).
- Ключ будь-якої LLM для голосу й вільного тексту — найпростіше безкоштовний Groq: <https://console.groq.com/keys>.

## 2. Встановлення
```bash
git clone https://github.com/vnazarenko-krikyxa/reminder_bot.git
cd reminder_bot
python -m venv venv
venv/bin/pip install -r requirements.txt        # Windows: venv\Scripts\pip install -r requirements.txt
cp .env.example .env                            # Windows: copy .env.example .env
```

## 3. Заповни `.env`
| Рядок | Звідки взяти |
|---|---|
| `TELEGRAM_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` → токен |
| `ALLOWED_USER_IDS` | свій id з [@userinfobot](https://t.me/userinfobot). **Обов'язково** — інакше бот відповідатиме будь-кому |
| `LLM_API_KEY` | ключ будь-якої LLM — див. нижче |

### Ключ LLM — без нього бот не розуміє голос і звичайні фрази
Без ключа на будь-яке повідомлення бот відповідає «Голос і вільний текст ще не працюють», працює лише покрокове `/add`.
Підходить ключ **будь-якої** з LLM нижче — впиши його в `.env` як `LLM_API_KEY=…`, провайдера бот визначить сам.

| Провайдер | Ключ починається з | Голос | Де взяти |
|---|---|---|---|
| **Groq** (радимо: безкоштовно) | `gsk_` | ✅ | <https://console.groq.com/keys> |
| OpenAI | `sk-proj-` | ✅ | <https://platform.openai.com/api-keys> |
| Anthropic (Claude) | `sk-ant-` | ➖ | <https://console.anthropic.com/settings/keys> |
| Google Gemini | `AIza` | ➖ | <https://aistudio.google.com/apikey> |
| OpenRouter | `sk-or-` | ➖ | <https://openrouter.ai/keys> |
| xAI (Grok) | `xai-` | ➖ | <https://console.x.ai> |
| DeepSeek, Mistral | `sk-…` / без префікса | ➖ | бот перевірить ключ запитом сам |

**Як отримати ключ Groq (безкоштовно):**
1. Зайди на <https://console.groq.com/keys> (вхід через Google).
2. **Create API Key** → назви, наприклад, `reminder-bot` → скопіюй ключ (`gsk_…`, показується один раз).
3. Встав у `.env`: `LLM_API_KEY=gsk_…` і перезапусти бота.

**Голос.** Розпізнають мову лише Groq і OpenAI. Якщо твій основний ключ іншого провайдера (наприклад, Claude) —
додай ще `STT_API_KEY=gsk_…` (безкоштовний ключ Groq) лише для голосу. Без нього працюватиме тільки текст.

**Додатково (необов'язково):**
- `LLM_MODEL` — своя модель замість типової (наприклад, `claude-haiku-5-5` — у рази дешевша за типову Claude).
- `LLM_PROVIDER` — якщо ключ не розпізнався (`groq`, `openai`, `anthropic`, `gemini`, `openrouter`, `xai`, `deepseek`, `mistral`).
- `LLM_BASE_URL` + `LLM_MODEL` — будь-який OpenAI-сумісний сервер (Ollama, LM Studio, корпоративний проксі).
- Стара назва `GROQ_API_KEY` теж працює.

У кожного — **свій** ключ: ліміти й оплата рахуються на ключ, спільний ключ ділив би їх на всіх.
Ключ нікому не пересилай і не комітай — `.env` у git не потрапляє.

## 4. Запуск
**Комп'ютер або сервер, що працює постійно (VPS):**
```bash
venv/bin/python bot.py
```
Для VPS зручно зробити systemd-сервіс з цією командою.

**Віртуальний хостинг з cron (Linux, без постійних процесів):** додай завдання cron щохвилини:
```
* * * * * /шлях/до/Reminder_bot/venv/bin/python /шлях/до/Reminder_bot/run_cron.py
```
Скрипт ~55 с слухає Telegram і завершується; наступний запуск підхоплює роботу. Якщо панель хостингу сама
підставляє свій Python, вкажи файл `start.py` — він перемкнеться на `venv`. Лог — `cron.log`.

### Перший запуск у Telegram
1. Відкрий свого бота й натисни `/start` — бот покаже, що налаштовано:
   ```
   ✅ Вільний текст: Groq
   ✅ Голос
   ➖ Робота: листи, «Не на місці», зустрічі (Google) — необов'язково
   ➖ Ник у листах не задано — змінити: /nick ТвійНік
   ```
   Якщо там ❌ або ⚠️ — перевір `LLM_API_KEY` у `.env` (бот підкаже, що саме не так) і перезапусти бота.
2. Задай свій ник для робочих листів: `/nick ТвійНік` — тема буде «ТвійНік / Відгул / 17.10.26».
   (Можна й у `.env`: `WORK_NICK=ТвійНік`; ник з `/nick` має пріоритет.)
3. Перевір голосом: «нагадай через 5 хвилин перевірити бота».

Свята України додаються самі, команди з'являються в меню «/» біля поля вводу.

## 5. Google (необов'язково): листи, «Не на місці», зустрічі
1. [Google Cloud Console](https://console.cloud.google.com) → створи проєкт (або візьми наявний).
2. **APIs & Services → Library** → увімкни **Gmail API**, **Google Calendar API**, **People API**.
3. **OAuth consent screen**: для робочого Google Workspace — тип **Internal** (інакше доступ злітає кожні 7 днів
   у режимі Testing).
4. **Credentials → Create credentials → OAuth client ID → Desktop app** → Download JSON →
   збережи як `google_client.json` у папці бота (у git не потрапляє).
5. У `.env` заповни `WORK_EMAIL_FROM`, `WORK_EMAIL_TO`, за бажанням `WORK_DAY` (ник — командою `/nick`).
6. На своєму комп'ютері (потрібен браузер):
   ```bash
   venv/bin/pip install google-auth-oauthlib
   venv/bin/python google_auth.py google_client.json
   ```
   Увійди в Google і натисни «Дозволити» — скрипт сам допише ключі в `.env`.
   Якщо бот працює на сервері — скопіюй туди оновлений `.env`.

Якщо адміністратор Workspace блокує сторонні застосунки — попроси дозволити свій OAuth-клієнт.

### Свої шаблони листів
Стандартні тексти — у `work.py` (`TEMPLATES`). Щоб змінити без правки коду, поклади поруч `work_templates.json`:
```json
{
  "day_off": ["{nick} / Day off / {dates}", "Hi all! Taking a day off on {dates}."],
  "sick": ["{nick} / Sick leave / {dates}", "Hi all! I'm not feeling well today."]
}
```
Плейсхолдери: `{nick}` `{date}` `{dates}` `{from}` `{to}`. Типи: `pause`, `late_start`, `early_finish`, `day_off`, `sick`.

## 6. Корисно знати
- **Дані:** усе зберігається в `reminders.db` (SQLite) поруч із ботом. Роби копію час від часу.
- **Приватність:** голосові й фрази обробляє обраний провайдер LLM (розпізнавання й розбір), листи й події — Google від твого імені.
- **Ліміти:** у безкоштовного Groq є ліміт токенів на хвилину — при частих запитах бот сам зачекає, повторить
  або візьме запасну модель.
- **Кілька людей в одному боті:** нагадування працюють для кожного з `ALLOWED_USER_IDS` окремо, але Google-функції
  прив'язані до одного акаунта. Для колег краще, щоб кожен запустив свою копію.
- **Тести:** `python -m unittest discover -s tests`.
