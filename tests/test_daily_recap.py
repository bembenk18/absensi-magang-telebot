import asyncio
import importlib.util
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytz
from telegram.constants import ChatType
from telegram.error import TelegramError

import config
import database
import main
import recap
import utils


def _load_isolated_config(env_overrides: dict[str, str]):
    """Load config.py into a fresh module with controlled os.environ and mocked load_dotenv."""
    spec = importlib.util.spec_from_file_location("_isolated_config", Path(config.__file__))
    mod = importlib.util.module_from_spec(spec)
    with patch("dotenv.load_dotenv", lambda *args, **kwargs: False):
        with patch.dict(os.environ, env_overrides, clear=True):
            assert spec.loader is not None
            spec.loader.exec_module(mod)
    return mod


class IsolatedRecapTestCase(unittest.IsolatedAsyncioTestCase):
    """Base test case isolating SQLite DB, reports/photos dirs, and config globals."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        tmp_path = Path(self.tmpdir.name)

        self.db_path = tmp_path / "test_attendance.db"
        self.photos_dir = tmp_path / "photos"
        self.reports_dir = tmp_path / "reports"
        self.photos_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)

        patches = [
            patch.object(database, "DB_PATH", self.db_path),
            patch.object(config, "DB_PATH", self.db_path),
            patch.object(config, "PHOTOS_DIR", self.photos_dir),
            patch.object(config, "REPORTS_DIR", self.reports_dir),
            patch.object(utils, "REPORTS_DIR", self.reports_dir),
            patch.object(config, "TIMEZONE", "Asia/Jakarta"),
            patch.object(config, "ADMIN_IDS", {9001, 9002}),
            patch.object(config, "RECAP_ENABLED", True),
            patch.object(config, "RECAP_HOUR", 18),
            patch.object(config, "RECAP_MINUTE", 0),
            patch.object(config, "RECAP_SKIP_WEEKENDS", False),
            patch.object(config, "RECAP_RECIPIENT_IDS", {9001, 9002}),
            patch.object(config, "RECAP_SEND_CSV", False),
            patch.object(config, "GOOGLE_SHEET_WEBHOOK_URL", ""),
            patch.object(config, "GOOGLE_SHEET_WEBHOOK_SECRET", ""),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        main._recap_last_failed_attempt.clear()
        self.addCleanup(main._recap_last_failed_attempt.clear)

        database.init_db()


class TestRecapConfig(unittest.TestCase):
    def test_parse_bool_env_variants(self):
        for truthy in ("true", "TRUE", " 1 ", "yes", "On"):
            with patch.dict(os.environ, {"TEST_BOOL": truthy}, clear=False):
                self.assertTrue(config._parse_bool_env("TEST_BOOL", False))

        for falsy in ("false", "FALSE", " 0 ", "no", "Off"):
            with patch.dict(os.environ, {"TEST_BOOL": falsy}, clear=False):
                self.assertFalse(config._parse_bool_env("TEST_BOOL", True))

        with patch.dict(os.environ, {"TEST_BOOL": "   "}, clear=False):
            self.assertTrue(config._parse_bool_env("TEST_BOOL", True))
            self.assertFalse(config._parse_bool_env("TEST_BOOL", False))

        with patch.dict(os.environ, {"TEST_BOOL": "invalid_bool"}, clear=False):
            with self.assertLogs("config", level="WARNING"):
                self.assertTrue(config._parse_bool_env("TEST_BOOL", True))

    def test_parse_int_env_bounds_and_invalid(self):
        with patch.dict(os.environ, {"TEST_HOUR": "0"}, clear=False):
            self.assertEqual(config._parse_int_env("TEST_HOUR", 18, 0, 23), 0)
        with patch.dict(os.environ, {"TEST_HOUR": "23"}, clear=False):
            self.assertEqual(config._parse_int_env("TEST_HOUR", 18, 0, 23), 23)

        for out_of_range in ("-1", "24", "999"):
            with patch.dict(os.environ, {"TEST_HOUR": out_of_range}, clear=False):
                with self.assertLogs("config", level="WARNING"):
                    self.assertEqual(config._parse_int_env("TEST_HOUR", 18, 0, 23), 18)

        with patch.dict(os.environ, {"TEST_MIN": "notanint"}, clear=False):
            with self.assertLogs("config", level="WARNING"):
                self.assertEqual(config._parse_int_env("TEST_MIN", 0, 0, 59), 0)

    def test_parse_id_set_filters_non_numeric(self):
        with self.assertLogs("config", level="WARNING"):
            parsed = config._parse_id_set(" 101, , 202 , abc, -5, 303 ", "RECAP_RECIPIENT_IDS")
        self.assertEqual(parsed, {101, 202, 303})
        self.assertEqual(config._parse_id_set("", "RECAP_RECIPIENT_IDS"), set())

    def test_recap_recipient_ids_fallback_and_overrides(self):
        mod_fallback = _load_isolated_config({
            "ADMIN_IDS": "111, 222",
            "RECAP_RECIPIENT_IDS": "",
        })
        self.assertEqual(mod_fallback.ADMIN_IDS, {111, 222})
        self.assertEqual(mod_fallback.RECAP_RECIPIENT_IDS, {111, 222})
        self.assertTrue(mod_fallback.RECAP_ENABLED)
        self.assertEqual(mod_fallback.RECAP_HOUR, 18)
        self.assertEqual(mod_fallback.RECAP_MINUTE, 0)
        self.assertFalse(mod_fallback.RECAP_SKIP_WEEKENDS)
        self.assertFalse(mod_fallback.RECAP_SEND_CSV)

        mod_override = _load_isolated_config({
            "ADMIN_IDS": "111, 222",
            "RECAP_RECIPIENT_IDS": "999, 888",
            "RECAP_HOUR": "25",
            "RECAP_MINUTE": "-3",
            "RECAP_SKIP_WEEKENDS": "true",
            "RECAP_SEND_CSV": "yes",
        })
        self.assertEqual(mod_override.RECAP_RECIPIENT_IDS, {999, 888})
        self.assertEqual(mod_override.RECAP_HOUR, 18)
        self.assertEqual(mod_override.RECAP_MINUTE, 0)
        self.assertTrue(mod_override.RECAP_SKIP_WEEKENDS)
        self.assertTrue(mod_override.RECAP_SEND_CSV)


class TestIsRecapDue(unittest.TestCase):
    def setUp(self):
        self.jkt = pytz.timezone("Asia/Jakarta")

    def test_before_at_after_target_time(self):
        # 2026-11-23 is Monday
        before = self.jkt.localize(datetime(2026, 11, 23, 17, 59, 59))
        at_time = self.jkt.localize(datetime(2026, 11, 23, 18, 0, 0))
        after = self.jkt.localize(datetime(2026, 11, 23, 21, 15, 0))

        self.assertFalse(recap.is_recap_due(before, True, 18, 0, False, False))
        self.assertTrue(recap.is_recap_due(at_time, True, 18, 0, False, False))
        self.assertTrue(recap.is_recap_due(after, True, 18, 0, False, False))

    def test_disabled_or_already_sent(self):
        after = self.jkt.localize(datetime(2026, 11, 23, 19, 0, 0))
        self.assertFalse(recap.is_recap_due(after, False, 18, 0, False, False))
        self.assertFalse(recap.is_recap_due(after, True, 18, 0, False, True))

    def test_skip_weekends_behavior(self):
        saturday = self.jkt.localize(datetime(2026, 11, 21, 18, 30, 0))  # Saturday
        sunday = self.jkt.localize(datetime(2026, 11, 22, 18, 30, 0))    # Sunday
        monday = self.jkt.localize(datetime(2026, 11, 23, 18, 30, 0))    # Monday

        self.assertEqual(saturday.weekday(), 5)
        self.assertEqual(sunday.weekday(), 6)
        self.assertEqual(monday.weekday(), 0)

        self.assertFalse(recap.is_recap_due(saturday, True, 18, 0, True, False))
        self.assertFalse(recap.is_recap_due(sunday, True, 18, 0, True, False))
        self.assertTrue(recap.is_recap_due(monday, True, 18, 0, True, False))

        self.assertTrue(recap.is_recap_due(saturday, True, 18, 0, False, False))
        self.assertTrue(recap.is_recap_due(sunday, True, 18, 0, False, False))

    def test_timezone_asia_jakarta_vs_utc(self):
        # 11:05 UTC == 18:05 WIB (Asia/Jakarta, UTC+7)
        utc_now = datetime(2026, 11, 23, 11, 5, 0, tzinfo=pytz.UTC)
        jkt_now = utc_now.astimezone(self.jkt)

        self.assertFalse(recap.is_recap_due(utc_now, True, 18, 0, False, False))
        self.assertTrue(recap.is_recap_due(jkt_now, True, 18, 0, False, False))

        with patch.object(config, "TIMEZONE", "Asia/Jakarta"):
            now_jkt = utils.get_now()
            self.assertEqual(now_jkt.utcoffset(), timedelta(hours=7))


class TestClassifyAndFormatRecap(unittest.TestCase):
    def test_classify_empty_students(self):
        classified = recap.classify_daily_attendance([], [])
        self.assertEqual(classified["total_students"], 0)
        for key in ("hadir_lengkap", "hadir_belum_pulang", "izin", "sakit", "libur", "belum_absen"):
            self.assertEqual(classified[key], [])
            self.assertEqual(classified["counts"][key], 0)
        self.assertEqual(classified["counts"]["non_student_records"], 0)

    def test_classify_all_buckets_and_non_student_row(self):
        students = [
            {"telegram_id": 1, "full_name": "Siswa Lengkap", "school": "SMKN 1", "nisn": "10001"},
            {"telegram_id": 2, "full_name": "Siswa Belum Pulang", "school": "SMKN 1", "nisn": "10002"},
            {"telegram_id": 3, "full_name": "Siswa Izin", "school": "SMKN 2", "nisn": "10003"},
            {"telegram_id": 4, "full_name": "Siswa Sakit", "school": "SMKN 2", "nisn": "10004"},
            {"telegram_id": 5, "full_name": "Siswa Libur", "school": "SMKN 3", "nisn": "10005"},
            {"telegram_id": 6, "full_name": "Siswa Belum Absen", "school": "SMKN 3", "nisn": "10006"},
        ]
        rows = [
            {
                "telegram_id": 1,
                "status": "Hadir",
                "check_in_office": "Kantor Pusat",
                "check_in_time": "07:55:00",
                "check_out_time": "17:05:00",
                "activity": "Testing",
            },
            {
                "telegram_id": 2,
                "status": "Hadir",
                "check_in_office": "Kantor Cabang",
                "check_in_time": "07:59:00",
                "check_out_time": None,
            },
            {"telegram_id": 3, "status": "Izin", "check_in_time": "-", "check_out_time": "-"},
            {"telegram_id": 4, "status": "Sakit", "check_in_time": "-", "check_out_time": "-"},
            {"telegram_id": 5, "status": "Libur", "check_in_time": "-", "check_out_time": "-"},
            # Non-student (e.g., admin) attendance record
            {
                "telegram_id": 9001,
                "full_name": "Admin Pembimbing",
                "school": "Internal",
                "status": "Hadir",
                "check_in_office": "Kantor Pusat",
                "check_in_time": "08:00:00",
                "check_out_time": "17:10:00",
                "activity": "Supervisi",
            },
        ]

        res = recap.classify_daily_attendance(students, rows)
        self.assertEqual(res["total_students"], 6)
        self.assertEqual(res["counts"]["hadir_lengkap"], 2)  # Siswa 1 + Admin 9001
        self.assertEqual(res["counts"]["hadir_belum_pulang"], 1)
        self.assertEqual(res["counts"]["izin"], 1)
        self.assertEqual(res["counts"]["sakit"], 1)
        self.assertEqual(res["counts"]["libur"], 1)
        self.assertEqual(res["counts"]["belum_absen"], 1)
        self.assertEqual(res["counts"]["non_student_records"], 1)

        non_students = [item for item in res["hadir_lengkap"] if item["is_non_student"]]
        self.assertEqual(len(non_students), 1)
        self.assertEqual(non_students[0]["full_name"], "Admin Pembimbing")
        self.assertEqual(res["belum_absen"][0]["full_name"], "Siswa Belum Absen")

    def test_format_html_and_markdown_escaping_and_counts(self):
        jkt = pytz.timezone("Asia/Jakarta")
        now = jkt.localize(datetime(2026, 11, 23, 18, 0, 0))
        students = [
            {
                "telegram_id": 10,
                "full_name": 'Budi <script>alert("x")</script> & *Bold* _Sub_',
                "school": "SMK <Maju> & `Dev`",
                "nisn": "12345",
            },
            {
                "telegram_id": 11,
                "full_name": "Siti *Bintang*",
                "school": "SMA 1",
                "nisn": "12346",
            },
        ]
        rows = [
            {
                "telegram_id": 10,
                "status": "Hadir",
                "check_in_office": 'Kantor <Pusat> & "Utama"',
                "check_in_time": "07:45:00",
                "check_out_time": "17:10:00",
                "activity": 'Fix <tag> & *markdown* _underscore_ `code` "quote"',
            }
        ]

        classified = recap.classify_daily_attendance(students, rows)
        msgs = recap.format_daily_recap_messages("2026-11-23", classified, now=now)
        self.assertEqual(len(msgs), 1)
        body = msgs[0]

        # Raw dangerous HTML must be escaped
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; *Bold* _Sub_", body)
        self.assertIn("SMK &lt;Maju&gt; &amp; `Dev`", body)
        self.assertIn("Kantor &lt;Pusat&gt; &amp; &quot;Utama&quot;", body)
        self.assertIn("Fix &lt;tag&gt; &amp; *markdown* _underscore_ `code` &quot;quote&quot;", body)

        # Summary counts
        self.assertIn("Total Siswa Terdaftar (Approved): <b>2</b>", body)
        self.assertIn("Hadir Lengkap (Masuk &amp; Pulang): <b>1</b>", body)
        self.assertIn("Belum Absen: <b>1</b>", body)
        self.assertIn("Siti *Bintang*", body)

    def test_format_empty_state_message(self):
        jkt = pytz.timezone("Asia/Jakarta")
        now = jkt.localize(datetime(2026, 11, 23, 18, 0, 0))
        classified = recap.classify_daily_attendance([], [])
        msgs = recap.format_daily_recap_messages("2026-11-23", classified, now=now)
        self.assertEqual(len(msgs), 1)
        self.assertIn("Belum ada siswa approved maupun data absensi", msgs[0])

    def test_format_splitting_custom_small_max_len_and_4096(self):
        jkt = pytz.timezone("Asia/Jakarta")
        now = jkt.localize(datetime(2026, 11, 23, 18, 0, 0))
        students = [
            {
                "telegram_id": i,
                "full_name": f"Siswa Ke {i:02d} Nama Panjang",
                "school": f"SMKN {i:02d} Jombang",
                "nisn": f"0000{i:02d}",
            }
            for i in range(1, 81)
        ]
        rows = [
            {
                "telegram_id": i,
                "status": "Hadir",
                "check_in_office": "Kantor Pusat",
                "check_in_time": "07:45:00",
                "check_out_time": "17:15:00",
                "activity": f"Melaksanakan tugas operasional harian batch ke-{i} dengan sangat rapi dan terstruktur",
            }
            for i in range(1, 61)
        ]

        classified = recap.classify_daily_attendance(students, rows)

        small_chunks = recap.format_daily_recap_messages("2026-11-23", classified, now=now, max_len=250)
        self.assertGreater(len(small_chunks), 5)
        for chunk in small_chunks:
            self.assertLessEqual(len(chunk), 250)

        std_chunks = recap.format_daily_recap_messages("2026-11-23", classified, now=now, max_len=4096)
        self.assertGreater(len(std_chunks), 1)
        for chunk in std_chunks:
            self.assertLessEqual(len(chunk), 4096)
        combined = "\n".join(std_chunks)
        self.assertIn("Siswa Ke 01 Nama Panjang", combined)
        self.assertIn("Siswa Ke 80 Nama Panjang", combined)

    def _assert_valid_html_chunk(self, chunk: str, max_len: int) -> None:
        class _BalancedHTMLValidator(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=False)
                self.stack: list[str] = []
                self.errors: list[str] = []

            def handle_starttag(self, tag, attrs):
                self.stack.append(tag.lower())

            def handle_endtag(self, tag):
                tag_l = tag.lower()
                if not self.stack:
                    self.errors.append(f"Unexpected closing tag </{tag_l}>")
                    return
                if self.stack[-1] != tag_l:
                    self.errors.append(f"Mismatched closing tag </{tag_l}>, expected </{self.stack[-1]}>")
                    return
                self.stack.pop()

            def handle_data(self, data):
                if "<" in data or ">" in data or "&" in data:
                    self.errors.append(f"Unescaped or truncated HTML char/entity in text: {data!r}")

            def handle_entityref(self, name):
                if name not in ("lt", "gt", "amp", "quot"):
                    self.errors.append(f"Unexpected entityref &{name};")

        self.assertLessEqual(len(chunk), max_len)
        stripped_entities = re.sub(r"&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);", "", chunk)
        self.assertNotIn("&", stripped_entities, f"Truncated entity found in chunk: {chunk!r}")

        parser = _BalancedHTMLValidator()
        parser.feed(chunk)
        parser.close()
        self.assertEqual(parser.errors, [], f"HTML errors in chunk {chunk!r}: {parser.errors}")
        self.assertEqual(parser.stack, [], f"Unclosed tags in chunk {chunk!r}: {parser.stack}")

    def test_format_tag_and_entity_safe_html_chunking_oversized_and_roster(self):
        jkt = pytz.timezone("Asia/Jakarta")
        now = jkt.localize(datetime(2026, 11, 23, 18, 0, 0))

        # 1. Single oversized student row (>4096 chars with '<' escaped as '&lt;' in full_name and school)
        oversized_students = [
            {
                "telegram_id": 1,
                "full_name": "<" * 900,
                "school": "<" * 300,
                "nisn": "99999",
            }
        ]
        oversized_rows = [
            {
                "telegram_id": 1,
                "status": "Hadir",
                "check_in_office": "Kantor <Pusat> & Cabang",
                "check_in_time": "07:50:00",
                "check_out_time": "17:05:00",
                "activity": "Kerja <aman> & lancar",
            }
        ]
        classified_oversized = recap.classify_daily_attendance(oversized_students, oversized_rows)
        chunks_4096 = recap.format_daily_recap_messages("2026-11-23", classified_oversized, now=now, max_len=4096)
        self.assertGreater(len(chunks_4096), 1)
        for chunk in chunks_4096:
            self._assert_valid_html_chunk(chunk, 4096)

        # Verify all escaped '<' entities survived without truncation
        joined_4096 = "".join(chunks_4096)
        self.assertGreaterEqual(joined_4096.count("&lt;"), 1200)

        # 2. Multi-student roster with HTML special characters at max_len=300
        roster_students = [
            {
                "telegram_id": i,
                "full_name": f"Siswa <{i:02d}> & Co",
                "school": f"SMK <Negeri> {i:02d} & Tim",
                "nisn": f"100{i:02d}",
            }
            for i in range(1, 21)
        ]
        roster_rows = [
            {
                "telegram_id": i,
                "status": "Hadir",
                "check_in_office": f"Kantor <{i}> & Utama",
                "check_in_time": "07:45:00",
                "check_out_time": "17:15:00" if i % 2 == 0 else None,
                "activity": f"Task <{i}> & 'quote' \"dbl\"",
            }
            for i in range(1, 16)
        ]
        classified_roster = recap.classify_daily_attendance(roster_students, roster_rows)
        chunks_300 = recap.format_daily_recap_messages("2026-11-23", classified_roster, now=now, max_len=300)
        self.assertGreater(len(chunks_300), 5)
        for chunk in chunks_300:
            self._assert_valid_html_chunk(chunk, 300)


class TestSendRecapAndScheduler(IsolatedRecapTestCase):
    def _seed_sample_day(self, date_str: str):
        database.register_user(101, "Ani Hadir", "SMKN 1", "100001", status="approved")
        database.register_user(102, "Budi Belum Pulang", "SMKN 1", "100002", status="approved")
        database.register_user(103, "Citra Izin", "SMKN 2", "100003", status="approved")
        database.register_user(104, "Dedi Belum Absen", "SMKN 2", "100004", status="approved")

        database.record_check_in(101, date_str, "07:50:00", -6.17, 106.82, 10.0, "a.jpg", "Kantor Pusat")
        database.record_check_out(101, date_str, "17:05:00", -6.17, 106.82, 12.0, "b.jpg", "Kantor Pusat", "Testing")
        database.record_check_in(102, date_str, "07:55:00", -6.17, 106.82, 15.0, "c.jpg", "Kantor Pusat")
        database.record_leave_attendance(103, date_str, "Izin", "surat.jpg")

    async def test_scheduler_dedup_same_day_and_simulated_restart_next_day(self):
        jkt = pytz.timezone("Asia/Jakarta")
        day1 = jkt.localize(datetime(2026, 11, 23, 18, 5, 0))
        day1_later = jkt.localize(datetime(2026, 11, 23, 18, 10, 0))
        day2 = jkt.localize(datetime(2026, 11, 24, 18, 0, 0))

        self._seed_sample_day("2026-11-23")
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.send_document = AsyncMock()

        res1 = await main.run_scheduler_tick(bot, now=day1)
        self.assertEqual(res1["recap_sent"], 2)
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertTrue(database.has_scheduled_job_run(recap.DAILY_RECAP_JOB_NAME, "2026-11-23"))

        # Repeated tick on same date should skip
        res2 = await main.run_scheduler_tick(bot, now=day1_later)
        self.assertEqual(res2["recap_sent"], 0)
        self.assertEqual(bot.send_message.await_count, 2)

        # Simulated process restart: clear in-memory dict, new bot instance, keep SQLite DB
        main._recap_last_failed_attempt.clear()
        fresh_bot = MagicMock()
        fresh_bot.send_message = AsyncMock()
        res_restart_same_day = await main.run_scheduler_tick(fresh_bot, now=day1_later)
        self.assertEqual(res_restart_same_day["recap_sent"], 0)
        self.assertEqual(fresh_bot.send_message.await_count, 0)

        # Next date sends a fresh recap
        res_next_day = await main.run_scheduler_tick(fresh_bot, now=day2)
        self.assertEqual(res_next_day["recap_sent"], 2)
        self.assertEqual(fresh_bot.send_message.await_count, 2)
        self.assertTrue(database.has_scheduled_job_run(recap.DAILY_RECAP_JOB_NAME, "2026-11-24"))

    async def test_total_delivery_failure_does_not_mark_sent_and_respects_cooldown(self):
        jkt = pytz.timezone("Asia/Jakarta")
        t0 = jkt.localize(datetime(2026, 11, 23, 18, 0, 0))
        t_within_cooldown = jkt.localize(datetime(2026, 11, 23, 18, 2, 0))
        t_after_cooldown = jkt.localize(datetime(2026, 11, 23, 18, 6, 0))

        self._seed_sample_day("2026-11-23")
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=TelegramError("network timeout"))

        res0 = await main.run_scheduler_tick(bot, now=t0)
        self.assertEqual(res0["recap_sent"], 0)
        self.assertFalse(database.has_scheduled_job_run(recap.DAILY_RECAP_JOB_NAME, "2026-11-23"))
        self.assertEqual(bot.send_message.await_count, 2)

        # Within 300s cooldown: should NOT retry
        res_cd = await main.run_scheduler_tick(bot, now=t_within_cooldown)
        self.assertEqual(res_cd["recap_sent"], 0)
        self.assertEqual(bot.send_message.await_count, 2)

        # After cooldown expires and Telegram recovers: retries and marks sent
        bot.send_message = AsyncMock()
        res_after = await main.run_scheduler_tick(bot, now=t_after_cooldown)
        self.assertEqual(res_after["recap_sent"], 2)
        self.assertTrue(database.has_scheduled_job_run(recap.DAILY_RECAP_JOB_NAME, "2026-11-23"))

    async def test_partial_recipient_failure_still_delivers_to_second_recipient(self):
        jkt = pytz.timezone("Asia/Jakarta")
        now = jkt.localize(datetime(2026, 11, 23, 18, 0, 0))
        self._seed_sample_day("2026-11-23")

        delivered_chats = []

        async def flaky_send(chat_id, text, parse_mode=None):
            if chat_id == 9001:
                raise TelegramError("Forbidden: bot was blocked by the user")
            delivered_chats.append((chat_id, text, parse_mode))

        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=flaky_send)

        sent = await recap.send_daily_recap(bot, date_str="2026-11-23", force=False, now=now)
        self.assertEqual(sent, 1)
        self.assertEqual(len(delivered_chats), 1)
        self.assertEqual(delivered_chats[0][0], 9002)
        self.assertEqual(delivered_chats[0][2], "HTML")
        self.assertTrue(database.has_scheduled_job_run(recap.DAILY_RECAP_JOB_NAME, "2026-11-23"))

    async def test_send_daily_recap_with_csv_attachment_and_cleanup(self):
        jkt = pytz.timezone("Asia/Jakarta")
        now = jkt.localize(datetime(2026, 11, 23, 18, 0, 0))
        self._seed_sample_day("2026-11-23")

        sent_docs = []

        async def fake_send_doc(chat_id, document, filename, caption, parse_mode=None):
            content = document.read()
            sent_docs.append((chat_id, filename, content, caption, parse_mode))

        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.send_document = AsyncMock(side_effect=fake_send_doc)

        with patch.object(config, "RECAP_SEND_CSV", True):
            sent = await recap.send_daily_recap(bot, date_str="2026-11-23", force=False, now=now)

        self.assertEqual(sent, 2)
        self.assertEqual(len(sent_docs), 2)
        self.assertTrue(sent_docs[0][1].startswith("rekap_harian_2026-11-23_"))
        self.assertIn(b"Ani Hadir", sent_docs[0][2])
        # Temp CSV must be cleaned up
        self.assertEqual(list(self.reports_dir.glob("*.csv")), [])

    async def test_recap_daily_command_auth_and_non_suppression(self):
        self._seed_sample_day("2026-11-23")

        # 1. Authorized admin in private chat
        admin_update = MagicMock()
        admin_update.effective_user = SimpleNamespace(id=9001, first_name="Admin")
        admin_update.effective_chat = SimpleNamespace(id=9001, type=ChatType.PRIVATE)
        admin_update.message = MagicMock()
        admin_update.message.reply_text = AsyncMock()

        ctx = MagicMock()
        ctx.bot = MagicMock()
        ctx.bot.send_message = AsyncMock()

        with patch.object(utils, "get_today_str", return_value="2026-11-23"):
            await main.recap_daily_command(admin_update, ctx)

        ctx.bot.send_message.assert_awaited_once()
        call_kwargs = ctx.bot.send_message.await_args.kwargs
        self.assertEqual(call_kwargs["chat_id"], 9001)
        self.assertEqual(call_kwargs["parse_mode"], "HTML")
        # Manual /rekapharian must NOT mark daily_recap as sent for scheduled run
        self.assertFalse(database.has_scheduled_job_run(recap.DAILY_RECAP_JOB_NAME, "2026-11-23"))

        # 2. Non-admin in private chat rejected
        student_update = MagicMock()
        student_update.effective_user = SimpleNamespace(id=101, first_name="Ani")
        student_update.effective_chat = SimpleNamespace(id=101, type=ChatType.PRIVATE)
        student_update.message = MagicMock()
        student_update.message.reply_text = AsyncMock()
        ctx.bot.send_message.reset_mock()

        await main.recap_daily_command(student_update, ctx)
        student_update.message.reply_text.assert_awaited_once()
        self.assertIn("khusus untuk Admin", student_update.message.reply_text.await_args.args[0])
        ctx.bot.send_message.assert_not_awaited()

        # 3. Non-private chat ignored
        group_update = MagicMock()
        group_update.effective_user = SimpleNamespace(id=9001, first_name="Admin")
        group_update.effective_chat = SimpleNamespace(id=-100123, type=ChatType.GROUP)
        group_update.message = MagicMock()
        group_update.message.reply_text = AsyncMock()

        await main.recap_daily_command(group_update, ctx)
        group_update.message.reply_text.assert_not_awaited()
        ctx.bot.send_message.assert_not_awaited()

    async def test_concurrent_run_scheduler_tick_executes_recap_only_once(self):
        jkt = pytz.timezone("Asia/Jakarta")
        now = jkt.localize(datetime(2026, 11, 23, 18, 5, 0))
        self._seed_sample_day("2026-11-23")

        total_send_calls = 0

        async def slow_send_message(chat_id, text, parse_mode=None):
            nonlocal total_send_calls
            await asyncio.sleep(0.02)
            total_send_calls += 1

        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=slow_send_message)
        bot.send_document = AsyncMock()

        res_a, res_b = await asyncio.gather(
            main.run_scheduler_tick(bot, now=now),
            main.run_scheduler_tick(bot, now=now),
        )

        self.assertEqual(sorted([res_a["recap_sent"], res_b["recap_sent"]]), [0, 2])
        self.assertEqual(total_send_calls, 2)
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertTrue(database.has_scheduled_job_run(recap.DAILY_RECAP_JOB_NAME, "2026-11-23"))


if __name__ == "__main__":
    unittest.main()
