# Як запустити свого бота

Кожен запускає **власну копію**: свій Telegram-бот, свій ключ Groq і (за бажанням) свій Google-акаунт.
Базові нагадування працюють без Google.

## 1. Що потрібно
- Python 3.11+
- Telegram-бот: у [@BotFather](https://t.me/BotFather) → `/newbot` → скопіюй токен.
- Свій Telegram id: напиши [@userinfobot](https://t.me/userinfobot).
- Ключ Groq для голосу й вільного тексту (безкоштовно): <https://console.groq.com/keys>.

## 2. Встановлення
```bash
git clone <цей репозиторій>
cd Reminder_bot
python -m venv venv
venv/bin/pip install -r requirements.txt        # Windows: venv\Scripts\pip install -r requirements.txt
cp .env.example .env
```
Заповни в `.env`: `TELEGRAM_TOKEN`, `ALLOWED_USER_IDS`, `GROQ_API_KEY`.
**Без `GROQ_API_KEY` бот не розуміє голос і звичайні фрази** — лише покрокове `/add`. Ключ безкоштовний,
у кожного свій (спільний ключ ділить на всіх ліміт запитів).
`ALLOWED_USER_IDS` — обов'язково, інакше бот відповідатиме будь-кому, хто його знайде.

## 3. Запуск
**Комп'ютер або сервер, що працює постійно (VPS):**
```bash
venv/bin/python bot.py
```
Для VPS зручно зробити systemd-сервіс з цією командою.

**Віртуальний хостинг з cron (Linux) (без постійних процесів):** додай завдання cron щохвилини:
```
* * * * * /шлях/до/Reminder_bot/venv/bin/python /шлях/до/Reminder_bot/run_cron.py
```
Скрипт ~55 с слухає Telegram і завершується; наступний запуск підхоплює роботу. Якщо панель хостингу сама
підставляє свій Python, вкажи файл `start.py` — він перемкнеться на `venv`. Лог — `cron.log`.

Відкрий бота в Telegram і натисни `/start` — бот покаже, що налаштовано (✅/❌). Задай свій ник для листів:
`/nick ТвійНік`. Далі — додадуться свята України, а команди з'являться в меню «/».

## 4. Google (необов'язково): листи, «Не на місці», зустрічі
1. [Google Cloud Console](https://console.cloud.google.com) → створи проєкт (або візьми наявний).
2. **APIs & Services → Library** → увімкни **Gmail API**, **Google Calendar API**, **People API**.
3. **OAuth consent screen**: для робочого Google Workspace — тип **Internal** (інакше доступ злітає кожні 7 днів
   у режимі Testing).
4. **Credentials → Create credentials → OAuth client ID → Desktop app** → Download JSON →
   збережи як `google_client.json` у папці бота (у git не потрапляє).
5. У `.env` заповни `WORK_EMAIL_FROM`, `WORK_EMAIL_TO`, за бажанням `WORK_NICK` і `WORK_DAY`.
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

## 5. Корисно знати
- **Дані:** усе зберігається в `reminders.db` (SQLite) поруч із ботом. Роби копію час від часу.
- **Приватність:** голосові й фрази обробляє Groq (розпізнавання й розбір), листи й події — Google від твого імені.
- **Ліміти Groq:** безкоштовний тариф має ліміт токенів на хвилину — при частих запитах бот сам зачекає й повторить.
- **Кілька людей в одному боті:** нагадування працюють для кожного з `ALLOWED_USER_IDS` окремо, але Google-функції
  прив'язані до одного акаунта. Для колег краще, щоб кожен запустив свою копію.
- **Тести:** `python -m unittest discover -s tests`.
