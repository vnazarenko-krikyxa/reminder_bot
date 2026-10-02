import sys, unittest
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import TZ
from meetings import find_slots, validate

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=TZ)  # пятница


def t(d, h, m=0):
    return datetime(2026, 9, d, h, m, tzinfo=TZ)


class Slots(unittest.TestCase):
    def test_skips_busy_and_weekend(self):
        busy = [(t(25, 15), t(25, 16)), (t(25, 16, 30), t(25, 19))]
        slots = find_slots(busy, t(25, 15), 45, NOW)
        # 15:00–16:00 занято, 16:00–16:45 не влезает до 16:30, дальше до 19 занято -> понедельник 9:00
        self.assertEqual(slots[0], t(28, 9))
        self.assertTrue(all(s.weekday() < 5 for s in slots))

    def test_first_free_same_day(self):
        slots = find_slots([(t(25, 15), t(25, 16))], t(25, 15), 45, NOW)
        self.assertEqual(slots[0], t(25, 16))

    def test_validate(self):
        self.assertIn("годину", validate({"attendees": ["Катя"]}, NOW)["error"])
        self.assertIn("з ким", validate({"time": "15:00"}, NOW)["error"])
        m = validate({"attendees": ["Катя"], "time": "15:00"}, NOW)
        self.assertEqual((m["duration_min"], m["meet"]), (45, True))


if __name__ == "__main__":
    unittest.main()
