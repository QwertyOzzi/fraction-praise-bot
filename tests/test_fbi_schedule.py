import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fbi_schedule import (
    ScheduleError, prepare_schedule, parse_ranks, parse_moscow_date, moscow_label,
    eligible_recipients, current_rank, job_message, next_pending, display_schedule,
    scheduled_tick, utc_iso
)

NOW = datetime(2026, 11, 5, 13, 0, tzinfo=timezone.utc)


class ScheduleDomainTests(unittest.TestCase):
    def test_parses_msk_not_server_timezone(self):
        self.assertEqual(parse_moscow_date("06.11.2026", "19:30", now=NOW),
                         datetime(2026, 11, 6, 16, 30, tzinfo=timezone.utc))
        self.assertEqual(moscow_label("2026-11-06T16:30:00+00:00"),
                         "06.11.2026 19:30 МСК")
        with self.assertRaises(ScheduleError):
            parse_moscow_date("06.11.2026", "99:01", now=NOW)
        with self.assertRaises(ScheduleError):
            parse_moscow_date("05.11.2026", "15:00", now=NOW)
        with self.assertRaises(ScheduleError):
            parse_moscow_date("01.04.2027", "19:00", now=NOW)

    def test_rank_ranges(self):
        self.assertEqual(parse_ranks("8-X"), (8, 10))
        self.assertEqual(parse_ranks("5"), (5, 5))
        self.assertEqual(current_rank("10 (Лидер)"), 10)
        self.assertEqual(current_rank("X"), 10)
        for value in ("0-9", "10-8", "some", "5-11"):
            with self.subTest(value=value), self.assertRaises(ScheduleError):
                parse_ranks(value)

    def test_event_reminders_skip_times_already_passed(self):
        event = prepare_schedule(
            "event", "Общий сбор", "На парковке", "06.11.2026", "13:00",
            "5-10", creator_id=42, reminders=(1440, 60, 15), now=NOW
        )
        self.assertEqual([x["id"] for x in event["jobs"]],
                         ["announcement", "before_60", "before_15"])
        self.assertEqual(event["kind"], "event")
        self.assertEqual(event["min_rank"], 5)
        self.assertIn("МСК", display_schedule(event))
        self.assertIn("Общий сбор", job_message(event, event["jobs"][0]))
        self.assertEqual(next_pending(event)["id"], "announcement")

    def test_scheduled_message_has_bounded_recurrence(self):
        for repeat, count in (("none", 1), ("daily", 4), ("weekly", 4)):
            schedule = prepare_schedule(
                "message", "Объявление", "Проверка поста",
                "07.11.2026", "19:00", "1-10", creator_id=42,
                repeat=repeat, now=NOW
            )
            self.assertEqual(len(schedule["jobs"]), count)
            self.assertIn("Проверка поста", job_message(schedule, schedule["jobs"][0]))
        with self.assertRaises(ScheduleError):
            prepare_schedule("message", "Новость", "Текст",
                             "20.12.2026", "19:00", "1-10",
                             creator_id=42, repeat="weekly", now=NOW)

    def test_audience_filters_rank_unverified_and_optout(self):
        profiles = {
            "1": {"nick": "Low", "rank": "3"},
            "2": {"nick": "High", "rank": "8"},
            "3": {"nick": "Leader", "rank": "X"},
            "4": {"nick": "Unverified", "rank": "9", "verification_status": "pending"},
            "5": {"nick": "OptOut", "rank": "8", "event_notifications": False},
            "6": {"nick": "Bad", "rank": "none"},
            "7": {"nick": "Gone", "rank": "9", "verification_status": "disconnected"},
        }
        self.assertEqual(eligible_recipients(profiles, 8, 10), [2, 3])
        self.assertEqual(eligible_recipients(profiles, 1, 3), [1])

    def test_invalid_details_fail_before_write(self):
        for title, desc in (("<", "Место встречи"), ("Нормальное название", "")):
            with self.assertRaises(ScheduleError):
                prepare_schedule("event", title, desc, "07.11.2026", "19:00",
                                 "8-X", creator_id=1, now=NOW)


class ScheduleWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_only_dispatches_claimed_due_jobs(self):
        item = prepare_schedule(
            "message", "Объявление", "Проверка сообщения",
            "06.11.2026", "18:00", "8-10", creator_id=42, now=NOW
        )
        current = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)
        recipients = {"1": {"nick": "Agent_A", "rank": "8"},
                      "2": {"nick": "Agent_B", "rank": "3"}}
        sent = []

        async def sender(uid, text):
            sent.append((uid, text))

        with patch("fbi_schedule.list_active", new=AsyncMock(return_value=[item])), \
                patch("fbi_schedule.claim_schedule_job", new=AsyncMock(return_value=(item, "claim"))), \
                patch("fbi_schedule.finish_schedule_job", new=AsyncMock(return_value=True)) as finish, \
                patch("fbi_schedule.asyncio.sleep", new=AsyncMock()):
            await scheduled_tick(object(), recipients, sender, now=current)
        self.assertEqual([uid for uid, _ in sent], [1])
        finish.assert_awaited_once()
        self.assertEqual(finish.await_args.args[-2:], (1, 0))

    async def test_worker_never_sends_without_claim(self):
        item = prepare_schedule("event", "Сбор агентов", "У мэрии",
                                "07.11.2026", "19:00", "1-10",
                                creator_id=42, now=NOW)
        sent = []
        async def sender(uid, text):
            sent.append(uid)
        with patch("fbi_schedule.list_active", new=AsyncMock(return_value=[item])), \
                patch("fbi_schedule.claim_schedule_job", new=AsyncMock(return_value=None)):
            await scheduled_tick(object(), {"1": {"nick": "A", "rank": "8"}},
                                 sender, now=NOW + timedelta(seconds=1))
        self.assertEqual(sent, [])


class ScheduleClaimTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_job_claimed_at_most_once_across_workers(self):
        from copy import deepcopy
        from fbi_schedule import claim_schedule_job
        item = prepare_schedule(
            "event", "Сбор состава", "Место старта",
            "06.11.2026", "18:00", "8-10", creator_id=42, now=NOW
        )
        state = {"item": item, "revision": 1}

        async def get(client, sid):
            return deepcopy(state["item"]), str(state["revision"])

        async def replace(client, changed, revision):
            if revision != str(state["revision"]):
                return False
            state["item"] = deepcopy(changed)
            state["revision"] += 1
            return True

        with patch("fbi_schedule.get_schedule", new=get), \
                patch("fbi_schedule._replace", new=replace):
            first = await claim_schedule_job(object(), item["id"],
                                             "announcement", now=NOW + timedelta(seconds=1))
            second = await claim_schedule_job(object(), item["id"],
                                              "announcement", now=NOW + timedelta(seconds=2))
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(state["item"]["jobs"][0]["state"], "claimed")


if __name__ == "__main__":
    unittest.main()
