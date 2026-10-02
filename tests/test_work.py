import os, sys, unittest
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import TZ
import work
from work import compose, ua_range, validate

work.WORK_NICK = "Nick"
from datetime import date

NOW = datetime(2026, 9, 25, 9, 30, tzinfo=TZ)


class Work(unittest.TestCase):
    def test_ranges(self):
        self.assertEqual(ua_range(date(2026, 9, 25), date(2026, 9, 25)), "25.09.26")
        self.assertEqual(ua_range(date(2026, 9, 28), date(2026, 9, 29)), "28-29.09.26")
        self.assertEqual(ua_range(date(2026, 9, 30), date(2026, 10, 1)), "30.09-01.10.26")

    def test_templates(self):
        c = compose({"kind": "pause", "date": "2026-09-25", "date_to": None, "from": "14:00", "to": "16:00"})
        self.assertEqual(c["subject"], "Nick / Пауза з 14:00 до 16:00 / 25.09.26")
        c = compose({"kind": "late_start", "date": "2026-09-26", "date_to": None, "from": "11:00", "to": None})
        self.assertEqual(c["subject"], "Nick / Старт робіт з 11:00 / 26.09.26")
        self.assertEqual((c["start"].hour, c["end"].hour), (0, 11))
        c = compose({"kind": "day_off", "date": "2026-09-28", "date_to": "2026-09-29", "from": None, "to": None})
        self.assertEqual(c["body"], "Всім привіт! Беру відгул на 28-29.09.26. Прошу відмітити за рахунок відпускних.")
        self.assertEqual(c["periods"], [(datetime(2026, 9, 28, 8, tzinfo=TZ), datetime(2026, 9, 28, 19, tzinfo=TZ)),
                                        (datetime(2026, 9, 29, 8, tzinfo=TZ), datetime(2026, 9, 29, 19, tzinfo=TZ))])

    def test_validation(self):
        self.assertIn("до котрої", validate({"kind": "pause", "from": "14:00"}, NOW)["error"])
        self.assertIn("минув", validate({"kind": "pause", "from": "7:00", "to": "8:00"}, NOW)["error"])
        a = validate({"kind": "sick"}, NOW)
        self.assertEqual((a["date"], a["date_to"]), ("2026-09-25", None))


if __name__ == "__main__":
    unittest.main()
