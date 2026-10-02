"""Праздники Украины: правила дат и два вида напоминаний.

gift=True  — про подарки/поздравления близких: блок за 30/7/1 день, в сам день — отдельное напоминание,
             если назначены получатели подарков.
gift=False — государственные/памятные: блок «завтра» и «сегодня».
"""
from datetime import date, timedelta

# rule, название, gift
HOLIDAYS = [
    ("fixed:01-01", "Новий рік", True),
    ("fixed:01-22", "День Соборності України", False),
    ("fixed:02-14", "День святого Валентина", True),
    ("fixed:03-08", "Міжнародний жіночий день", True),
    ("easter", "Великдень", True),
    ("easter+49", "Трійця", False),
    ("fixed:05-08", "День пам'яті та перемоги над нацизмом", False),
    ("weekday:05-2-6", "День матері", True),       # 2-га неділя травня
    ("weekday:06-3-6", "День батька", True),       # 3-тя неділя червня
    ("fixed:06-28", "День Конституції України", False),
    ("fixed:07-15", "День Української Державності", False),
    ("fixed:08-24", "День Незалежності України", False),
    ("fixed:10-01", "День захисників і захисниць України", False),
    ("fixed:11-21", "День Гідності та Свободи", False),
    ("fixed:12-06", "День Святого Миколая", True),
    ("fixed:12-06/zsu", "День Збройних Сил України", False),
    ("fixed:12-25", "Різдво Христове", True),
]
GIFT = {rule: gift for rule, _, gift in HOLIDAYS}
BLOCK_DAYS_GIFT = (30, 7, 1)   # за сколько дней показывать «подарочные» праздники в блоке
BLOCK_DAYS_STATE = (1, 0)      # государственные — завтра и сегодня


def orthodox_easter(year: int) -> date:
    """Православная Пасха (алгоритм Мёза для юлианского календаря + разница календарей)."""
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month, day = divmod(d + e + 114, 31)
    julian = date(year, month, day + 1)
    return julian + timedelta(days=13)  # для 1900–2099


def in_year(rule: str, year: int) -> date:
    if rule.startswith("fixed:"):
        mm, dd = rule[6:11].split("-")
        return date(year, int(mm), int(dd))
    if rule.startswith("easter"):
        shift = int(rule[7:]) if rule.startswith("easter+") else 0
        return orthodox_easter(year) + timedelta(days=shift)
    if rule.startswith("weekday:"):
        mm, nth, wd = map(int, rule[8:].split("-"))
        first = date(year, mm, 1)
        return first + timedelta(days=(wd - first.weekday()) % 7 + 7 * (nth - 1))
    raise ValueError(rule)


def next_date(rule: str, today: date) -> date:
    d = in_year(rule, today.year)
    return d if d >= today else in_year(rule, today.year + 1)


def title(rule: str) -> str:
    return next(t for r, t, _ in HOLIDAYS if r == rule)
