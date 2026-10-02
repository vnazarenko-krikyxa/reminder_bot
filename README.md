# Reminder bot

Telegram-бот-нагадувалка з голосовим керуванням: дні народження, свята України, справи й бажання,
ідеї та історія подарунків, а також робочі листи про відсутність і зустрічі в Google Calendar.

- **[Що вміє бот](FEATURES.md)**
- **[Як запустити свого](SETUP.md)**

Стек: Python 3.11+, [python-telegram-bot](https://python-telegram-bot.org/), SQLite,
Groq (Whisper + gpt-oss) для голосу й розбору фраз, Google APIs (Gmail, Calendar, People) — за бажанням.

## Структура
| Файл | Що робить |
|---|---|
| `bot.py` | Telegram: команди, кнопки, картки, планувальник |
| `run_cron.py`, `start.py` | запуск на хостингу з cron (без постійного процесу) |
| `reminders.py` | коли й що нагадувати, вигляд повідомлень |
| `db.py` | SQLite: події, повтори, люди, ідеї, подарунки |
| `ai.py` | Groq: голос → текст, фраза → дія |
| `ua_holidays.py` | свята України (зокрема Великдень) |
| `work.py` | листи про відсутність і шаблони |
| `meetings.py`, `google_api.py`, `google_auth.py` | зустрічі, Gmail/Calendar, одноразова авторизація Google |
