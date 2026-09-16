import copy
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import bot
import state_store


class BotTests(unittest.TestCase):
    def setUp(self):
        self.config = bot.read_json(bot.ROOT / "config.json")
        self.cities = bot.read_json(bot.ROOT / "cities.json")
        self.now = datetime(2026, 9, 13, 12, tzinfo=bot.BERLIN)
        self.row = {
            "site": "indeed", "title": "Junior Embedded Developer", "company": "Demo",
            "location": "Stendal, Germany", "description": "STM32. 30–40 Stunden pro Woche.",
            "job_type": "parttime", "date_posted": "2026-09-12", "job_url": "https://de.indeed.com/viewjob?jk=abcd1234"
        }

    def evaluate(self, **changes):
        return bot.evaluate_job({**self.row, **changes}, self.config, self.cities, self.now)

    def test_hours_units_range_decimals_and_nonweekly_numbers(self):
        self.assertEqual(bot.weekly_hours("€60/hour; project total 60 hours; 30–40 Stunden pro Woche; 38,5 h/Woche"), [(30, 40), (38.5, 38.5)])
        self.assertEqual(bot.weekly_hours("30 bis 35 Std. wöchentlich"), [(30, 35)])
        self.assertEqual(bot.weekly_hours("40 hours per week"), [(40, 40)])
        self.assertEqual(bot.weekly_hours("60 Stunden Projektumfang, 30 Urlaubstage, €50/h"), [])

    def test_hours_overlap_unknown_and_part_time_independent(self):
        self.assertIsNotNone(self.evaluate(description="STM32, 20–40 h/week")[0])
        self.assertEqual(self.evaluate(description="STM32, 20 h/week")[1], "hours_outside_range")
        self.assertIsNotNone(self.evaluate(description="STM32")[0])
        self.config["filters"]["allow_unknown_hours"] = False
        self.assertEqual(self.evaluate(description="STM32")[1], "unknown_hours")
        self.config["filters"]["part_time_only"] = True
        self.assertEqual(self.evaluate(job_type="fulltime")[1], "not_part_time")

    def test_seniority_checked_in_title_not_mentors_description(self):
        self.assertEqual(self.evaluate(title="Senior Embedded Developer")[1], "senior")
        self.assertIsNotNone(self.evaluate(description="Mentoring by senior engineers. STM32. 40 h/week")[0])
        self.assertEqual(self.evaluate(description="STM32, at least 5 years of experience")[1], "experience_over_2")

    def test_student_role_toggle_and_enrolment(self):
        self.assertEqual(self.evaluate(title="Werkstudent Firmware")[1], "student_role")
        self.assertEqual(self.evaluate(description="STM32; must be currently enrolled")[1], "enrolment_required")
        self.config["filters"]["include_student_roles"] = True
        self.assertIsNotNone(self.evaluate(title="Werkstudent Firmware")[0])

    def test_real_location_not_search_or_mentions(self):
        self.assertEqual(self.evaluate(location="München", description="STM32; our client in Berlin")[1], "unmapped_location")
        self.assertIsNotNone(self.evaluate(location="Berlin, Germany")[0])
        self.assertEqual(bot.resolve_city("Berlin-Spandau", self.cities)["name"], "Berlin-Spandau")
        self.assertIsNone(bot.resolve_city("Berlinchen", self.cities))
        self.config["filters"]["local_radius_km"] = 10
        self.assertEqual(self.evaluate(location="Tangermünde")[1], "outside_region")
        self.assertIsNotNone(self.evaluate(location="Magdeburg")[0])

    def test_freshness_and_unknown_date(self):
        self.assertEqual(self.evaluate(date_posted="2020-01-01")[1], "outside_date_window")
        self.assertIn("не підтверджена", self.evaluate(date_posted=None)[0]["posted"])
        self.config["filters"]["require_known_date"] = True
        self.assertEqual(self.evaluate(date_posted=None)[1], "unknown_date")

    def test_language_flag_optional_filter(self):
        self.assertTrue(self.evaluate(description="STM32. Fluent German required.")[0]["warnings"])
        self.assertTrue(self.evaluate(description="STM32. Fließende Deutschkenntnisse.")[0]["warnings"])
        self.config["filters"]["exclude_explicit_german_above_a2"] = True
        self.assertEqual(self.evaluate(description="STM32. German B2 required.")[1], "german_above_a2")
        self.assertIsNotNone(self.evaluate(description="STM32. Deutsch A2, English B2.")[0])

    def test_url_identity_and_validation(self):
        a = bot.canonical_url("https://www.indeed.com/rc/clk?jk=abcd1234&utm_source=email")
        self.assertEqual(a, self.row["job_url"])
        self.assertEqual(bot.canonical_url("https://de.linkedin.com/jobs/view/junior-embedded-123456/?trackingId=abc"), "https://www.linkedin.com/jobs/view/123456")
        self.assertIsNone(bot.canonical_url("https://linkedin.com.evil.example/jobs/view/123456"))
        self.assertIsNone(bot.canonical_url("javascript:alert(1)"))

    def test_telegram_utf16_plain_text(self):
        job = self.evaluate(title="Junior Embedded <Developer> & STM32")[0]
        job["transport"] = {"text": "\U0001f680" * 10000}
        text = bot.message_for(job)
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 3500)
        with patch("bot.fetch_json", return_value={"ok": True, "result": {"message_id": 1}}) as request:
            bot.Telegram("123:fake_test_token", "456").send(text, job["url"])
        self.assertNotIn("parse_mode", request.call_args.args[1])

    def test_telegram_rate_limit_and_no_secret_in_errors(self):
        error = HTTPError("https://api.telegram.org/bot123:secret/sendMessage", 429, "Too Many", {}, io.BytesIO(b'{"ok":false,"error_code":429,"parameters":{"retry_after":1}}'))
        with patch("bot.fetch_json", side_effect=[error, {"ok": True, "result": {"message_id": 1}}]) as call, patch("bot.time.sleep"):
            bot.Telegram("123:secret", "456").send("test")
        self.assertEqual(call.call_count, 2)
        with patch("bot.fetch_json", side_effect=URLError("https://api.telegram.org/bot123:secret/sendMessage")) as call:
            with self.assertRaises(bot.ServiceError) as raised:
                bot.Telegram("123:secret", "456").send("test")
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(call.call_count, 1)

    def test_partial_delivery_checkpoint_and_rerun(self):
        a = self.evaluate()[0]
        b = self.evaluate(job_url="https://de.indeed.com/viewjob?jk=efgh5678")[0]
        for job in (a, b):
            job["transport"] = {"text": "Test route"}
        class Sender:
            def __init__(self):
                self.calls = 0
            def send(self, *args):
                self.calls += 1
                if self.calls == 2:
                    raise bot.ServiceError("delivery failed")
        with tempfile.TemporaryDirectory() as directory, patch("bot.time.sleep"):
            path = Path(directory) / "sent.json"
            state = bot.load_state(path)
            sender = Sender()
            with self.assertRaises(bot.ServiceError):
                bot.deliver([a, b], state, path, sender, self.now, 10)
            restored = bot.load_state(path)
            self.assertIn(a["key"], restored["sent"])
            self.assertNotIn(b["key"], restored["sent"])
            with patch.object(sender, "send") as send:
                self.assertEqual(bot.deliver([a, b], restored, path, sender, self.now, 10), 1)
                send.assert_called_once()

    def test_message_limit_does_not_mark_pending_as_sent(self):
        a = self.evaluate()[0]
        b = self.evaluate(job_url="https://de.indeed.com/viewjob?jk=efgh5678")[0]
        for job in (a, b):
            job["transport"] = {"text": "Test route"}
        with tempfile.TemporaryDirectory() as directory, patch("bot.time.sleep"), patch.object(bot.Telegram, "send"):
            state = {"version": 1, "sent": {}}
            bot.deliver([a, b], state, Path(directory) / "state.json", bot.Telegram("123:fake", "456"), self.now, 1)
            self.assertNotIn(b["key"], state["sent"])

    def test_damaged_state_is_not_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sent.json"
            bot.write_json(path, {"version": 5, "sent": {}})
            with self.assertRaises(bot.ServiceError):
                bot.load_state(path)

    def test_state_push_uses_expected_sha_and_noop_has_no_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path, meta_path = Path(directory) / "state.json", Path(directory) / "meta.json"
            original = {"version": 1, "sent": {}}
            changed = {"version": 1, "sent": {"a" * 64: self.now.isoformat()}}
            bot.write_json(state_path, changed)
            bot.write_json(meta_path, {"sha": "previous-blob-sha", "original": original})
            with patch.object(state_store, "STATE", state_path), patch.object(state_store, "META", meta_path), patch.object(state_store, "api") as call:
                state_store.push()
                self.assertEqual(call.call_args.args[1]["sha"], "previous-blob-sha")
                self.assertEqual(call.call_args.args[1]["branch"], "bot-state")
                bot.write_json(state_path, original)
                call.reset_mock()
                state_store.push()
                call.assert_not_called()


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.config = bot.read_json(bot.ROOT / "config.json")["transport"]
        self.day = datetime(2026, 9, 14, tzinfo=bot.BERLIN).date()

    def journey(self, dep="07:20", arr="08:30", product="bus", cancelled=False):
        return {"legs": [{"departure": bot.at_time(self.day, dep).isoformat(), "arrival": bot.at_time(self.day, arr).isoformat(), "cancelled": cancelled,
                          "line": {"name": "Bus 900", "product": product}}]}

    def test_workday_uses_local_zone_and_skips_weekend(self):
        self.assertEqual(bot.next_workday(datetime(2026, 9, 11, 23, tzinfo=bot.BERLIN)).isoformat(), "2026-09-14")

    def test_morning_includes_buffers_and_early_arrival_wait(self):
        result = bot.choose_journey([self.journey()], bot.at_time(self.day, "09:00"), "out", self.config, 120)
        self.assertEqual(result["minutes"], 110)
        self.assertIsNone(bot.choose_journey([self.journey(arr="08:50")], bot.at_time(self.day, "09:00"), "out", self.config, 180))

    def test_return_includes_wait_until_bus_and_rejects_early_bus(self):
        result = bot.choose_journey([self.journey("18:00", "19:00")], bot.at_time(self.day, "17:00"), "back", self.config, 180)
        self.assertEqual(result["minutes"], 130)
        self.assertIsNone(bot.choose_journey([self.journey("17:00", "18:00")], bot.at_time(self.day, "17:00"), "back", self.config, 180))

    def test_cancelled_ice_and_taxi_not_eligible(self):
        for journey in [self.journey(cancelled=True), self.journey(product="nationalExpress"), self.journey(product="taxi")]:
            self.assertIsNone(bot.choose_journey([journey], bot.at_time(self.day, "09:00"), "out", self.config, 180))

    def test_api_failure_is_unknown_not_accessible(self):
        transport = bot.Transport(self.config)
        city = {"name": "Stendal", "lat": 52.606, "lon": 11.859, "stop": "Stendal Hbf"}
        with patch("bot.fetch_json", side_effect=URLError("network unavailable")) as call:
            result = transport.check({"city": city, "local": True})
            self.assertEqual(result["status"], "unknown")
            transport.check({"city": {**city, "name": "other"}, "local": True})
            self.assertEqual(call.call_count, 1)

    def test_transport_requires_both_directions(self):
        transport = bot.Transport(self.config, datetime(2026, 9, 13, tzinfo=bot.BERLIN))
        city = {"name": "Stendal", "lat": 52.606, "lon": 11.859, "stop": "Stendal Hbf"}
        with patch.object(transport, "stop", side_effect=[{"id":"a","name":"Havelberg"}, {"id":"b","name":"Stendal"}]), patch.object(transport, "get", side_effect=[{"journeys":[self.journey()]}, {"journeys": []}]):
            self.assertEqual(transport.check({"city": city, "local": True})["status"], "no_match")


if __name__ == "__main__":
    unittest.main()
