"""Отсутствие на работе: письмо команде по шаблону + событие «Не на місці» в календаре."""
import json
from datetime import date, datetime, time, timedelta

from config import BASE_DIR, TZ, WORK_DAY, WORK_NICK

# Відгул / лікарняний: «Не на місці» на кожен день у ці години (WORK_DAY=08:00-19:00)
FULL_DAY = tuple(time.fromisoformat(x) for x in WORK_DAY.split("-"))

# Шаблони листів. Плейсхолдери: {nick} {date} {dates} {from} {to}.
# Свої тексти — у work_templates.json поруч (той самий формат), див. SETUP.md.
TEMPLATES = {
    "pause": ("{nick} / Пауза з {from} до {to} / {date}",
              "Всім привіт! Буду на паузі з {from} до {to}. В ТГ частково доступний."),
    "late_start": ("{nick} / Старт робіт з {from} / {date}",
                   "Всім привіт! Почну працювати з {from}. В ТГ частково доступний."),
    "early_finish": ("{nick} / Робота до {to} / {date}",
                     "Всім привіт! Сьогодні закінчую раніше {to}. Час відпрацюю."),
    "day_off": ("{nick} / Відгул / {dates}",
                "Всім привіт! Беру відгул на {dates}. Прошу відмітити за рахунок відпускних."),
    "sick": ("{nick} / Лікарняний / {dates}",
             "Всім привіт! Погано себе почуваю. Прошу відмітити за рахунок лікарняного."),
}
_custom = BASE_DIR / "work_templates.json"
if _custom.exists():
    TEMPLATES.update({k: tuple(v) for k, v in json.loads(_custom.read_text(encoding="utf-8")).items()})

KINDS = {
    "pause": "⏸ Пауза",
    "late_start": "🌅 Старт робіт пізніше",
    "early_finish": "🌇 Робота до",
    "day_off": "🏖 Відгул",
    "sick": "🤒 Лікарняний",
}


def ua_date(d: date) -> str:
    return f"{d:%d.%m.%y}"


def ua_range(d1: date, d2: date) -> str:
    """28.09.26 · 28-29.09.26 · 30.09-01.10.26 · 30.12.26-02.01.27"""
    if d1 == d2:
        return ua_date(d1)
    if (d1.year, d1.month) == (d2.year, d2.month):
        return f"{d1:%d}-{ua_date(d2)}"
    if d1.year == d2.year:
        return f"{d1:%d.%m}-{ua_date(d2)}"
    return f"{ua_date(d1)}-{ua_date(d2)}"


def compose(a: dict) -> dict:
    """a — {"kind", "date", "date_to", "from", "to"} (даты ISO, время HH:MM).

    Возвращает тему, текст письма и период события [start, end) в TZ.
    """
    d1 = date.fromisoformat(a["date"])
    d2 = date.fromisoformat(a["date_to"]) if a.get("date_to") else d1
    t_from, t_to = a.get("from"), a.get("to")
    kind = a["kind"]
    day_start = lambda d: datetime.combine(d, time(0), tzinfo=TZ)  # noqa: E731
    at = lambda d, t: datetime.combine(d, time.fromisoformat(t), tzinfo=TZ)  # noqa: E731

    if kind == "pause":
        start, end = at(d1, t_from), at(d1, t_to)
    elif kind == "late_start":
        start, end = day_start(d1), at(d1, t_from)
    elif kind == "early_finish":
        start, end = at(d1, t_to), day_start(d1 + timedelta(days=1))
    elif kind in ("day_off", "sick"):
        start, end = at(d1, f"{FULL_DAY[0]:%H:%M}"), at(d2, f"{FULL_DAY[1]:%H:%M}")
    else:
        raise ValueError(f"неизвестный тип {kind!r}")
    # ник: свій у боті (/nick) -> з .env (WORK_NICK)
    fields = {"nick": a.get("nick") or WORK_NICK, "date": ua_date(d1), "dates": ua_range(d1, d2), "from": t_from, "to": t_to}
    subject, body = (t.format(**fields) for t in TEMPLATES[kind])
    subject = subject.lstrip(" /")  # без ніка — тема без «/» на початку
    if kind in ("day_off", "sick"):
        # окрема подія на кожен день, щоб ночі й вечори не були «Не на місці»
        days = [d1 + timedelta(days=i) for i in range((d2 - d1).days + 1)]
        periods = [(at(d, f"{FULL_DAY[0]:%H:%M}"), at(d, f"{FULL_DAY[1]:%H:%M}")) for d in days]
    else:
        periods = [(start, end)]
    return {"subject": subject, "body": body, "start": start, "end": end, "periods": periods}


def validate(d: dict, now: datetime) -> dict:
    """Проверяет absence от модели. Возвращает нормализованный dict или {"error": ...}."""
    kind = d.get("kind")
    if kind not in KINDS:
        raise ValueError(f"kind={kind!r}")
    from ai import parse_time  # локальный импорт: ai импортирует work
    today = now.date()
    d1 = date.fromisoformat(d.get("date") or today.isoformat())
    d2 = date.fromisoformat(d["date_to"]) if d.get("date_to") else None
    if d2 and d2 < d1:
        d1, d2 = d2, d1
    a = {"kind": kind, "date": d1.isoformat(), "date_to": d2.isoformat() if d2 and d2 != d1 else None,
         "from": parse_time(d.get("from")), "to": parse_time(d.get("to"))}
    if kind == "pause":
        if not a["from"]:
            return {"error": "з котрої години пауза?"}
        if not a["to"]:
            return {"error": f"пауза з {a['from']} — до котрої?"}
        a["date_to"] = None  # пауза всегда в пределах одного дня
    elif kind == "late_start":
        if not a["from"]:
            return {"error": "з котрої почнеш працювати?"}
        a["to"], a["date_to"] = None, None
    elif kind == "early_finish":
        if not a["to"]:
            return {"error": "до котрої працюєш?"}
        a["from"], a["date_to"] = None, None
    else:
        a["from"] = a["to"] = None
    c = compose(a)
    if c["end"] <= c["start"]:
        return {"error": "кінець раніше за початок — перевір час"}
    last_day = date.fromisoformat(a["date_to"] or a["date"])
    if (kind in ("day_off", "sick") and last_day < today) or (kind not in ("day_off", "sick") and c["end"] <= now):
        return {"error": "цей період вже минув"}
    return a
