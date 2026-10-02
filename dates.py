"""Работа с датами событий: разбор ввода, ближайшая дата, 29 февраля."""
import calendar
import re
from datetime import date

MONTHS_GEN = [
    "", "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
]


def occurrence_date(month: int, day: int, year: int) -> date:
    """Дата события в конкретном году; 29.02 в невисокосный год -> 28.02."""
    if month == 2 and day == 29 and not calendar.isleap(year):
        return date(year, 2, 28)
    return date(year, month, day)


def next_yearly(month: int, day: int, today: date) -> date:
    """Ближайшая (сегодня или позже) дата ежегодного события."""
    d = occurrence_date(month, day, today.year)
    if d < today:
        d = occurrence_date(month, day, today.year + 1)
    return d


def parse_date(text: str):
    """'14.03', '14.03.1990', '14/3/90', '1990-03-14' -> (day, month, year|None).

    Бросает ValueError, если дата некорректна.
    """
    text = text.strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})[./\-\s](\d{1,2})(?:[./\-\s](\d{2}|\d{4}))?", text)
        if not m:
            raise ValueError("не схоже на дату")
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else None
        if y is not None and y < 100:
            # 90 -> 1990, 05 -> 2005
            y += 2000 if y <= date.today().year % 100 + 1 else 1900
    # проверяем день/месяц (для 29.02 без года берём високосный год)
    date(y if y else 2000, mo, d)
    return d, mo, y


def fmt_day_month(d: date) -> str:
    return f"{d.day} {MONTHS_GEN[d.month]}"


def plural_days(n: int) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} дні"
    return f"{n} днів"


def plural_years(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} рік"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} роки"
    return f"{n} років"
