"""Встречи с коллегами: разбор, поиск свободного времени, текст карточки."""
import re
from datetime import date, datetime, time, timedelta

from config import TZ

DEFAULT_MINUTES = 45
# Окно, в котором бот ищет альтернативные слоты, если кто-то занят (будни)
SLOT_DAY = (time(9, 0), time(19, 0))
SLOT_STEP = timedelta(minutes=15)
EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")


def validate(d: dict, now: datetime) -> dict:
    """Проверяет meeting от модели. Возвращает нормализованный dict или {"error": ...}."""
    from ai import parse_time  # ai импортирует этот модуль
    attendees = [str(a).strip() for a in d.get("attendees") or [] if str(a).strip()]
    m = {
        "title": (str(d["title"]).strip()[:200] or None) if d.get("title") else None,
        "date": d.get("date") or now.date().isoformat(),
        "time": parse_time(d.get("time")),
        "duration_min": int(d.get("duration_min") or DEFAULT_MINUTES),
        "attendees": attendees,
        "meet": d.get("meet") is not False,
        "notes": (str(d["notes"]).strip()[:1000] or None) if d.get("notes") else None,
    }
    date.fromisoformat(m["date"])
    m["duration_min"] = max(5, min(m["duration_min"], 8 * 60))
    if not attendees:
        return {"error": "з ким зустріч?"}
    if not m["time"]:
        return {"error": "на котру годину?"}
    if start_of(m) <= now:
        return {"error": f"{m['time']} {'сьогодні ' if m['date'] == now.date().isoformat() else ''}вже минуло — на коли перенести?"}
    return m


def start_of(m: dict) -> datetime:
    return datetime.combine(date.fromisoformat(m["date"]), time.fromisoformat(m["time"]), tzinfo=TZ)


def is_email(s: str) -> bool:
    return bool(EMAIL_RE.match(s))


def overlaps(busy: list, start: datetime, end: datetime) -> list:
    return [(s, e) for s, e in busy if s < end and e > start]


def find_slots(busy: list, start: datetime, minutes: int, now: datetime, count: int = 3, days: int = 5) -> list:
    """Ближайшие свободные для всех слоты начиная с start (будни, SLOT_DAY, шаг 15 мин)."""
    dur = timedelta(minutes=minutes)
    step_start = max(start, now + SLOT_STEP)
    # округляем вверх до шага
    step_start = step_start.replace(second=0, microsecond=0)
    step_start += timedelta(minutes=(-step_start.minute) % 15)
    found = []
    day = step_start.date()
    for _ in range(days * 3):
        if len(found) >= count or (day - start.date()).days > days + 2:
            break
        if day.weekday() < 5:
            t = max(step_start, datetime.combine(day, SLOT_DAY[0], tzinfo=TZ))
            day_end = datetime.combine(day, SLOT_DAY[1], tzinfo=TZ)
            while t + dur <= day_end and len(found) < count:
                if not overlaps(busy, t, t + dur):
                    found.append(t)
                    t += dur  # следующий вариант — не впритык к предыдущему
                else:
                    t += SLOT_STEP
        day += timedelta(days=1)
    return found


def default_title(people: list) -> str:
    names = ", ".join(p["name"].split()[0] for p in people)
    return f"Зустріч: {names}"


def validate_change(d: dict, now: datetime) -> dict:
    """Проверяет meeting_change от модели: что ищем и (для переноса) куда переносим."""
    from ai import parse_time
    kind = d.get("change")
    if kind not in ("cancel", "move"):
        raise ValueError(f"change={kind!r}")
    f = d.get("find") or {}
    find = {
        "attendees": [str(a).strip() for a in f.get("attendees") or [] if str(a).strip()],
        "date": f.get("date") or None,
        "time": parse_time(f.get("time")),
        "title": (str(f["title"]).strip() or None) if f.get("title") else None,
    }
    if find["date"]:
        date.fromisoformat(find["date"])
    n = d.get("new") or {}
    new = {
        "date": n.get("date") or None,
        "time": parse_time(n.get("time")),
        "duration_min": int(n["duration_min"]) if n.get("duration_min") else None,
    }
    if new["date"]:
        date.fromisoformat(new["date"])
    if not (find["attendees"] or find["date"] or find["time"] or find["title"]):
        return {"error": "яку зустріч? Скажи, з ким або коли вона"}
    if kind == "move" and not (new["date"] or new["time"] or new["duration_min"]):
        return {"error": "на коли перенести?"}
    return {"kind": kind, "find": find, "new": new}
