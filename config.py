import os
from datetime import time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Минимальный .env-загрузчик, чтобы не тянуть лишнюю зависимость."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
# Кому разрешено пользоваться ботом (Telegram user id через запятую). Пусто = всем.
ALLOWED_USER_IDS = {
    int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
}
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Kyiv"))
DB_PATH = Path(os.environ.get("DB_PATH", BASE_DIR / "reminders.db"))

# За сколько дней напоминать (0 = в сам день)
REMIND_OFFSETS = [30, 14, 7, 1, 0]
# Во сколько утренние напоминания и во сколько повторять «поздравил?» в день события
REMIND_AT = time.fromisoformat(os.environ.get("REMIND_TIME", "10:30"))
EVENING_HOUR = int(os.environ.get("EVENING_HOUR", "19"))
# Повтор «сегодняшних» напоминаний без реакции: каждые 2 ч, до 3 раз, с 9:00 и до итога дня
REPEAT_EVERY = timedelta(hours=2)
REPEAT_MAX = 3
REPEAT_FROM_HOUR = 9
# Вечерний итог «не отмечено сегодня»; после него — тишина до утра
DIGEST_HOUR = 20
# За сколько дней до события начинать ежедневно дёргать, если подарок не куплен
GIFT_NAG_DAYS = 7
# Как часто бот проверяет, пора ли что-то отправить (секунды)
TICK_SECONDS = 60

# Groq: распознавание голоса и разбор фраз в событие
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_STT_MODEL = "whisper-large-v3-turbo"
GROQ_MODELS = ["openai/gpt-oss-20b", "openai/gpt-oss-120b"]
MAX_VOICE_SECONDS = 120

# Отсутствие на работе: письмо команде + «Не на місці» в Google Calendar (см. google_auth.py)
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
WORK_EMAIL_FROM = os.environ.get("WORK_EMAIL_FROM", "")
WORK_EMAIL_TO = os.environ.get("WORK_EMAIL_TO", "")
# Підпис у темі листа («Nick / Пауза …») і робочі години для відгулу/лікарняного
WORK_NICK = os.environ.get("WORK_NICK", "")
WORK_DAY = os.environ.get("WORK_DAY", "08:00-19:00")
