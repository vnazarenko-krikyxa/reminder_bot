import sys, unittest
from datetime import date, datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import TZ
from dates import next_yearly, parse_date
from reminders import decide


def occ(d, gift=1, congrats=1, gift_done=0, congrats_done=0, snoozed=None, time=None,
        created="2026-01-01 00:00:00", recurrence=None):
    return dict(id=1, date=d, needs_gift=gift, needs_congrats=congrats, gift_done=gift_done,
                congrats_done=congrats_done, snoozed_until=snoozed, status="active",
                time=time, created_at=created, acted_on=None, kind=None, rule=None,
                recurrence=recurrence or ("yearly" if gift or congrats else "once"))


def at(y, m, d, h=11, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=TZ)


class Dates(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_date("14.03"), (14, 3, None))
        self.assertEqual(parse_date("14.03.1990"), (14, 3, 1990))
        self.assertEqual(parse_date("1990-03-14"), (14, 3, 1990))
        self.assertEqual(parse_date("29.02"), (29, 2, None))
        with self.assertRaises(ValueError):
            parse_date("31.02")

    def test_feb29(self):
        self.assertEqual(next_yearly(2, 29, date(2027, 1, 1)), date(2027, 2, 28))
        self.assertEqual(next_yearly(2, 29, date(2028, 1, 1)), date(2028, 2, 29))

    def test_rolls_to_next_year(self):
        self.assertEqual(next_yearly(3, 14, date(2026, 9, 23)), date(2027, 3, 14))


class Decide(unittest.TestCase):
    def test_offsets(self):
        o = occ("2026-10-23")
        self.assertEqual(decide(o, at(2026, 9, 23), set()), "d30")
        self.assertIsNone(decide(o, at(2026, 9, 23), {"d30"}))
        self.assertIsNone(decide(o, at(2026, 9, 23, 8), set()))  # рано утром молчим

    def test_gift_nag_daily_last_week(self):
        o = occ("2026-10-01")
        self.assertEqual(decide(o, at(2026, 9, 26), set()), "nag:2026-09-26")
        o["gift_done"] = 1
        self.assertIsNone(decide(o, at(2026, 9, 26), set()))

    def test_gift_done_skips_far_offsets_but_not_day0(self):
        o = occ("2026-10-23", gift_done=1)
        self.assertIsNone(decide(o, at(2026, 9, 23), set()))
        o["date"] = "2026-09-23"
        self.assertEqual(decide(o, at(2026, 9, 23), set()), "d0")
        self.assertEqual(decide(o, at(2026, 9, 23, 19), {"d0"}), "d0:evening")

    def test_late_and_all_done(self):
        o = occ("2026-09-22", gift_done=1)
        self.assertEqual(decide(o, at(2026, 9, 23), set()), "late")
        o["congrats_done"] = 1
        self.assertIsNone(decide(o, at(2026, 9, 23), set()))

    def test_snooze(self):
        o = occ("2026-09-26", snoozed="2026-09-24T10:00:00+03:00")
        self.assertIsNone(decide(o, at(2026, 9, 23, 12), {"d7"}))
        key = decide(o, at(2026, 9, 24, 10), set())
        self.assertTrue(key.startswith("snooze:"))
        # после повторного — в тот же день больше не дёргаем, назавтра снова по расписанию
        self.assertIsNone(decide(o, at(2026, 9, 24, 11), {key}))
        self.assertEqual(decide(o, at(2026, 9, 25), {key}), "d1")


class Timed(unittest.TestCase):
    def task(self, **kw):
        # «Напомни сегодня в 20:00 купить плитку», создано в 14:00 по Киеву (11:00 UTC)
        return occ("2026-09-24", gift=0, congrats=0, time="20:00", created="2026-09-24 11:00:00", **kw)

    def test_fires_at_exact_time_only(self):
        o = self.task()
        self.assertIsNone(decide(o, at(2026, 9, 24, 14, 1), set()))   # утреннего «d0» нет
        self.assertIsNone(decide(o, at(2026, 9, 24, 19, 59), set()))  # и за 2 часа не дёргаем — задача свежая
        self.assertEqual(decide(o, at(2026, 9, 24, 20, 0), set()), "at")
        self.assertIsNone(decide(o, at(2026, 9, 24, 20, 1), {"at"}))

    def test_even_before_morning_hour(self):
        o = occ("2026-09-25", gift=0, congrats=0, time="07:30", created="2026-09-24 11:00:00")
        self.assertIsNone(decide(o, at(2026, 9, 24, 12), set()))  # «Завтра» для свежей задачи не шлём
        self.assertEqual(decide(o, at(2026, 9, 25, 7, 30), set()), "at")

    def test_far_event_gets_days_and_pre(self):
        o = occ("2026-12-12", gift=0, congrats=0, time="19:00", created="2026-09-01 10:00:00")
        self.assertEqual(decide(o, at(2026, 12, 5), set()), "d7")
        self.assertIsNone(decide(o, at(2026, 12, 12, 10), set()))       # утром в сам день — ждём
        self.assertEqual(decide(o, at(2026, 12, 12, 17), set()), "pre")
        self.assertEqual(decide(o, at(2026, 12, 12, 19), {"pre"}), "at")

    def test_snooze_30_then_nothing_more(self):
        o = self.task(snoozed="2026-09-24T20:30:00+03:00")
        self.assertIsNone(decide(o, at(2026, 9, 24, 20, 10), {"at"}))
        key = decide(o, at(2026, 9, 24, 20, 30), {"at"})
        self.assertTrue(key.startswith("snooze:"))
        self.assertIsNone(decide(o, at(2026, 9, 24, 20, 31), {"at", key}))


class AdvanceNotice(unittest.TestCase):
    def card(self, key, **kw):
        from reminders import render
        o = occ("2026-10-14", **kw)
        o.update(title="повезти малого до лікаря", year=2026, notes=None)
        return render(o, date(2026, 10, 13), key)

    def test_task_tomorrow_has_no_buttons(self):
        text, kb = self.card("d1", gift=0, congrats=0)
        self.assertIn("Завтра", text)
        self.assertIsNone(kb)

    def test_day_of_and_gift_keep_buttons(self):
        self.assertIsNotNone(self.card(None, gift=0, congrats=0)[1])
        self.assertIsNotNone(self.card("d1", gift=1, congrats=0)[1])


if __name__ == "__main__":
    unittest.main()
