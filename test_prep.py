"""Tests for the look-ahead prep engine: rule detection, seed idempotency,
and prep-state round-trips. Pure logic + SQLite only, no LLM calls.

Run: python -m unittest test_prep -v
(DATABASE_URL is pointed at a temp file before db is imported.)
"""
import os
import tempfile
import unittest

_TMPDIR = tempfile.mkdtemp(prefix="kya_banao_test_")
os.environ["DATABASE_URL"] = os.path.join(_TMPDIR, "test.db")

import db  # noqa: E402  (must come after DATABASE_URL is set)
from llm import _detect_prep_from_rules  # noqa: E402

CHAT_ID = -999001


class PrepRuleDetectionTest(unittest.TestCase):
    def setUp(self):
        db.init_db()
        self.rules = db.get_prep_rules()

    def test_seed_populates_rules(self):
        patterns = {r["pattern"] for r in self.rules}
        self.assertIn("rajma", patterns)
        self.assertIn("tikka", patterns)

    def test_seed_is_idempotent(self):
        before = len(db.get_prep_rules())
        db.init_db()  # second boot
        self.assertEqual(len(db.get_prep_rules()), before)

    def test_rajma_needs_overnight_soak(self):
        meals = {"breakfast": "Poha", "lunch": "Rajma chawal", "dinner": "Veg pulao"}
        matched, unmatched = _detect_prep_from_rules(meals, self.rules, lead="night_before")
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["meal_type"], "lunch")
        self.assertIn("soak", matched[0]["action"].lower())
        # Poha and pulao fall through to the LLM fallback pool
        self.assertEqual({u["dish"] for u in unmatched}, {"Poha", "Veg pulao"})

    def test_detection_is_case_insensitive(self):
        meals = {"breakfast": "", "lunch": "RAJMA Masala", "dinner": None}
        matched, _ = _detect_prep_from_rules(meals, self.rules, lead="night_before")
        self.assertEqual(len(matched), 1)

    def test_no_prep_day_matches_nothing(self):
        meals = {"breakfast": "Upma", "lunch": "Curd rice", "dinner": "Khichdi"}
        matched, unmatched = _detect_prep_from_rules(meals, self.rules, lead="night_before")
        self.assertEqual(matched, [])
        self.assertEqual(len(unmatched), 3)

    def test_same_day_lead_is_separate(self):
        meals = {"breakfast": "Toast", "lunch": "Paneer tikka wrap", "dinner": "Dal"}
        night, _ = _detect_prep_from_rules(meals, self.rules, lead="night_before")
        morning, _ = _detect_prep_from_rules(meals, self.rules, lead="same_day_morning")
        self.assertEqual(night, [])
        self.assertEqual(len(morning), 1)
        self.assertIn("marinade", morning[0]["action"].lower())

    def test_empty_meals_dict(self):
        matched, unmatched = _detect_prep_from_rules({}, self.rules, lead="night_before")
        self.assertEqual(matched, [])
        self.assertEqual(unmatched, [])


class PrepStateTest(unittest.TestCase):
    MEAL_DATE = "2026-07-14"

    def setUp(self):
        db.init_db()
        with db.conn() as c:
            c.execute("DELETE FROM prep_state WHERE chat_id = ?", (CHAT_ID,))

    def _nudge(self):
        db.record_prep_nudges(CHAT_ID, self.MEAL_DATE, [
            {"meal_type": "lunch", "dish": "Rajma chawal", "action": "Soak the rajma tonight"},
            {"meal_type": "dinner", "dish": "Dal makhani", "action": "Soak the whole urad tonight"},
        ])

    def test_nudge_records_state(self):
        self._nudge()
        state = db.get_prep_state(CHAT_ID, self.MEAL_DATE)
        self.assertEqual(len(state), 2)
        self.assertTrue(all(s["status"] == "nudged" for s in state))

    def test_rerun_does_not_duplicate(self):
        self._nudge()
        self._nudge()  # cron re-run / manual /prep after cron
        self.assertEqual(len(db.get_prep_state(CHAT_ID, self.MEAL_DATE)), 2)

    def test_confirm_one_dish_by_hint(self):
        self._nudge()
        hit = db.set_prep_status(CHAT_ID, self.MEAL_DATE, "confirmed", dish_hint="rajma")
        self.assertEqual(hit, 1)
        by_dish = {s["dish"]: s["status"] for s in db.get_prep_state(CHAT_ID, self.MEAL_DATE)}
        self.assertEqual(by_dish["Rajma chawal"], "confirmed")
        self.assertEqual(by_dish["Dal makhani"], "nudged")

    def test_miss_all_without_hint(self):
        self._nudge()
        hit = db.set_prep_status(CHAT_ID, self.MEAL_DATE, "missed")
        self.assertEqual(hit, 2)
        self.assertTrue(all(
            s["status"] == "missed" for s in db.get_prep_state(CHAT_ID, self.MEAL_DATE)
        ))

    def test_update_with_no_tracked_prep_hits_zero(self):
        hit = db.set_prep_status(CHAT_ID, "2099-01-01", "confirmed")
        self.assertEqual(hit, 0)


if __name__ == "__main__":
    unittest.main()
