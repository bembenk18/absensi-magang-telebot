import csv
import hashlib
import hmac
import importlib.util
import logging
import math
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytz
from telegram.constants import ChatType
from telegram.ext import ConversationHandler

import config
import database
import main
import sheets_sync
import utils


class IsolatedCoreTestCase(unittest.IsolatedAsyncioTestCase):
    """Base test case isolating SQLite DB, photos/reports dirs, and config globals."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        tmp_path = Path(self.tmpdir.name)

        self.db_path = tmp_path / "test_attendance.db"
        self.photos_dir = tmp_path / "photos"
        self.reports_dir = tmp_path / "reports"
        self.photos_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)

        self.test_offices = [
            {"name": "Kantor <Pusat> & Utama", "lat": -6.175392, "lon": 106.827153, "radius": 100.0},
            {"name": "Kantor Cabang", "lat": -6.200000, "lon": 106.850000, "radius": 50.0},
        ]

        patches = [
            patch.object(database, "DB_PATH", self.db_path),
            patch.object(config, "DB_PATH", self.db_path),
            patch.object(config, "PHOTOS_DIR", self.photos_dir),
            patch.object(config, "REPORTS_DIR", self.reports_dir),
            patch.object(utils, "REPORTS_DIR", self.reports_dir),
            patch.object(config, "TIMEZONE", "Asia/Jakarta"),
            patch.object(config, "ADMIN_IDS", {9001, 9002}),
            patch.object(config, "CHECKOUT_OPEN_HOUR", 17),
            patch.object(config, "CHECKOUT_OPEN_MINUTE", 0),
            patch.object(config, "OFFICES", self.test_offices),
            patch.object(config, "GOOGLE_SHEET_WEBHOOK_URL", ""),
            patch.object(config, "GOOGLE_SHEET_WEBHOOK_SECRET", ""),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        database.init_db()


class TestUtilsSecurityAndCore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.reports_dir = Path(self.tmpdir.name) / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)

        p1 = patch.object(config, "REPORTS_DIR", self.reports_dir)
        p2 = patch.object(utils, "REPORTS_DIR", self.reports_dir)
        p3 = patch.object(config, "TIMEZONE", "Asia/Jakarta")
        p4 = patch.object(config, "CHECKOUT_OPEN_HOUR", 17)
        p5 = patch.object(config, "CHECKOUT_OPEN_MINUTE", 0)
        for p in (p1, p2, p3, p4, p5):
            p.start()
            self.addCleanup(p.stop)

        self.jkt = pytz.timezone("Asia/Jakarta")

    def test_work_schedule_days_1_to_12_vs_13_plus(self):
        for day in (1, 12):
            dt = self.jkt.localize(datetime(2026, 11, day, 7, 30, 0))
            sched = utils.get_work_schedule(dt)
            self.assertEqual(sched["entry_hour"], 8)
            self.assertEqual(sched["entry_minute"], 0)
            self.assertEqual(sched["reminder_hour"], 7)
            self.assertEqual(sched["entry_time_str"], "08:00")

        for day in (13, 30):
            dt = self.jkt.localize(datetime(2026, 11, day, 8, 30, 0))
            sched = utils.get_work_schedule(dt)
            self.assertEqual(sched["entry_hour"], 9)
            self.assertEqual(sched["entry_minute"], 0)
            self.assertEqual(sched["reminder_hour"], 8)
            self.assertEqual(sched["entry_time_str"], "09:00")

    def test_is_checkout_open(self):
        before = self.jkt.localize(datetime(2026, 11, 23, 16, 59, 59))
        at_open = self.jkt.localize(datetime(2026, 11, 23, 17, 0, 0))
        after = self.jkt.localize(datetime(2026, 11, 23, 23, 59, 0))

        self.assertFalse(utils.is_checkout_open(before)[0])
        self.assertTrue(utils.is_checkout_open(at_open)[0])
        self.assertTrue(utils.is_checkout_open(after)[0])

    def test_calculate_distance_and_antipodal_clamp(self):
        self.assertEqual(utils.calculate_distance(-6.175392, 106.827153, -6.175392, 106.827153), 0.0)

        antipodal_eq = utils.calculate_distance(0.0, 0.0, 0.0, 180.0)
        self.assertTrue(math.isfinite(antipodal_eq))
        self.assertGreater(antipodal_eq, 20_000_000.0)

        antipodal_poles = utils.calculate_distance(90.0, 0.0, -90.0, 0.0)
        self.assertTrue(math.isfinite(antipodal_poles))
        self.assertGreater(antipodal_poles, 20_000_000.0)

    def test_check_proximity_all_offices_fail_closed_and_match(self):
        invalid_offices = [
            {"name": "Zero", "lat": -6.175392, "lon": 106.827153, "radius": 0},
            {"name": "Negative", "lat": -6.175392, "lon": 106.827153, "radius": -50},
            {"name": "NaN", "lat": -6.175392, "lon": 106.827153, "radius": float("nan")},
            {"name": "BadType", "lat": -6.175392, "lon": 106.827153, "radius": "invalid"},
        ]
        res_fail = utils.check_proximity_all_offices(-6.175392, 106.827153, invalid_offices)
        self.assertFalse(res_fail["is_valid"])
        self.assertIsNone(res_fail["matched_office"])
        self.assertTrue(all(not d["is_inside"] for d in res_fail["details"]))

        offices = [
            {"name": "Kantor A", "lat": -6.175392, "lon": 106.827153, "radius": 100.0},
            {"name": "Kantor B", "lat": -6.200000, "lon": 106.850000, "radius": 50.0},
        ]
        inside = utils.check_proximity_all_offices(-6.175392, 106.827153, offices)
        self.assertTrue(inside["is_valid"])
        self.assertEqual(inside["matched_office"]["name"], "Kantor A")

        outside = utils.check_proximity_all_offices(-7.0, 107.0, offices)
        self.assertFalse(outside["is_valid"])
        self.assertIsNotNone(outside["closest_office"])

    def test_generate_csv_report_formula_injection_neutralization(self):
        records = [
            {
                "date": "2026-11-23",
                "full_name": "=CMD|' /C calc'!A0",
                "school": "+SUM(1,1)",
                "nisn": "-12345",
                "check_in_office": "@evil",
                "check_in_time": "\t08:00:00",
                "check_in_distance": 5.0,
                "check_out_office": "\rKantor",
                "check_out_time": " =1+1",
                "check_out_distance": 6.0,
                "activity": "Normal activity",
                "status": "Hadir",
            }
        ]
        csv_path = utils.generate_csv_report(records, filename_prefix="test_sec")
        self.assertTrue(Path(csv_path).is_file())

        with open(csv_path, mode="r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["Nama Lengkap"], "'=CMD|' /C calc'!A0")
        self.assertEqual(row["Asal Sekolah"], "'+SUM(1,1)")
        self.assertEqual(row["NISN"], "'-12345")
        self.assertEqual(row["Kantor Masuk"], "'@evil")
        self.assertEqual(row["Jam Masuk"], "'\t08:00:00")
        self.assertEqual(row["Kantor Pulang"], "'\rKantor")
        self.assertEqual(row["Jam Pulang"], "' =1+1")
        self.assertEqual(row["Laporan Kegiatan"], "Normal activity")
        self.assertEqual(row["Status"], "Hadir")

    def test_geofence_nan_inf_oob_bool_fail_closed(self):
        valid_office = [{"name": "Kantor Valid", "lat": -6.175392, "lon": 106.827153, "radius": 100.0}]

        # 1. calculate_distance rejects NaN, inf, -inf, out-of-range, and bool
        bad_coords = [
            (float("nan"), 106.8, -6.17, 106.8),
            (-6.17, float("nan"), -6.17, 106.8),
            (-6.17, 106.8, float("inf"), 106.8),
            (-6.17, 106.8, -6.17, float("-inf")),
            (90.0001, 106.8, -6.17, 106.8),
            (-90.0001, 106.8, -6.17, 106.8),
            (-6.17, 180.0001, -6.17, 106.8),
            (-6.17, -180.0001, -6.17, 106.8),
            (True, 106.8, -6.17, 106.8),
            (-6.17, False, -6.17, 106.8),
            (-6.17, 106.8, True, 106.8),
        ]
        for coords in bad_coords:
            with self.assertRaises(ValueError, msg=f"Expected ValueError for {coords}"):
                utils.calculate_distance(*coords)

        # 2. check_proximity_all_offices fails closed on NaN/inf/OOB/bool user coords
        for bad_lat, bad_lon in (
            (float("nan"), 106.827153),
            (-6.175392, float("nan")),
            (float("inf"), 106.827153),
            (-6.175392, float("-inf")),
            (91.0, 106.827153),
            (-91.0, 106.827153),
            (-6.175392, 181.0),
            (-6.175392, -181.0),
            (True, 106.827153),
            (-6.175392, False),
        ):
            res = utils.check_proximity_all_offices(bad_lat, bad_lon, valid_office)
            self.assertFalse(res["is_valid"])
            self.assertIsNone(res["matched_office"])
            self.assertIsNone(res["closest_office"])
            self.assertIsNone(res["nearest_office"])
            self.assertEqual(res["distance"], float("inf"))
            self.assertEqual(res["min_distance"], float("inf"))
            self.assertEqual(res["details"], [])

        # 3. check_proximity_all_offices skips offices with NaN/inf/OOB coords or radius <= 0
        bad_offices = [
            {"name": "NaN Lat", "lat": float("nan"), "lon": 106.827153, "radius": 100.0},
            {"name": "Inf Lon", "lat": -6.175392, "lon": float("inf"), "radius": 100.0},
            {"name": "OOB Lat", "lat": 95.0, "lon": 106.827153, "radius": 100.0},
            {"name": "OOB Lon", "lat": -6.175392, "lon": -185.0, "radius": 100.0},
            {"name": "Bool Lat", "lat": True, "lon": 106.827153, "radius": 100.0},
            {"name": "Zero Rad", "lat": -6.175392, "lon": 106.827153, "radius": 0},
            {"name": "Neg Rad", "lat": -6.175392, "lon": 106.827153, "radius": -25},
            {"name": "Inf Rad", "lat": -6.175392, "lon": 106.827153, "radius": float("inf")},
            {"name": "Bool Rad", "lat": -6.175392, "lon": 106.827153, "radius": True},
        ]
        res_bad_offices = utils.check_proximity_all_offices(-6.175392, 106.827153, bad_offices)
        self.assertFalse(res_bad_offices["is_valid"])
        self.assertIsNone(res_bad_offices["matched_office"])
        self.assertEqual(res_bad_offices["details"], [])

        res_mixed = utils.check_proximity_all_offices(-6.175392, 106.827153, bad_offices + valid_office)
        self.assertTrue(res_mixed["is_valid"])
        self.assertEqual(res_mixed["matched_office"]["name"], "Kantor Valid")
        self.assertEqual(len(res_mixed["details"]), 1)

        # 4. config._validate_office_entry rejects NaN/inf/OOB/bool/radius<=0
        invalid_entries = [
            ("NaN Lat", float("nan"), 106.8, 100),
            ("Inf Lon", -6.17, float("inf"), 100),
            ("-Inf Rad", -6.17, 106.8, float("-inf")),
            ("OOB Lat", 90.5, 106.8, 100),
            ("OOB Lat Neg", -90.5, 106.8, 100),
            ("OOB Lon", -6.17, 180.5, 100),
            ("OOB Lon Neg", -6.17, -180.5, 100),
            ("Zero Rad", -6.17, 106.8, 0),
            ("Neg Rad", -6.17, 106.8, -10),
            ("Bool Lat", True, 106.8, 100),
            ("Bool Rad", -6.17, 106.8, False),
        ]
        with self.assertLogs("config", level="WARNING"):
            for name, lat, lon, rad in invalid_entries:
                self.assertIsNone(config._validate_office_entry(name, lat, lon, rad, f"test:{name}"))

        valid_entry = config._validate_office_entry("Kantor", "-6.175392", "106.827153", "150", "test:valid")
        self.assertEqual(valid_entry, {"name": "Kantor", "lat": -6.175392, "lon": 106.827153, "radius": 150.0})

        # Isolated config loading skips invalid OFFICES JSON entries and falls back safely
        spec = importlib.util.spec_from_file_location("_isolated_config_geo", Path(config.__file__))
        mod = importlib.util.module_from_spec(spec)
        bad_json = '[{"name":"BadOOB","lat":95,"lon":106.8,"radius":100},{"name":"BadRad","lat":-6.2,"lon":106.8,"radius":0}]'
        with patch("dotenv.load_dotenv", lambda *args, **kwargs: False):
            with patch.dict(os.environ, {"OFFICES": bad_json, "OFFICE_LATITUDE": "NaN", "OFFICE_LONGITUDE": "106.8"}, clear=True):
                assert spec.loader is not None
                spec.loader.exec_module(mod)
        self.assertEqual(len(mod.OFFICES), 1)
        self.assertTrue(math.isfinite(mod.OFFICES[0]["lat"]))
        self.assertGreater(mod.OFFICES[0]["radius"], 0)

    def test_extended_csv_formula_neutralization(self):
        sneaky_formulas = [
            "\n=1",
            "\x0b=1",
            "\x0c=1",
            "\u00a0=1",
            "\u200b=1",
            "\ufeff+1",
            '""=1',
            "\u200c-1",
            "\u200d@SUM(1,2)",
            '""\u00a0=CMD()',
        ]
        for payload in sneaky_formulas:
            sanitized = utils.sanitize_csv_cell(payload)
            self.assertEqual(sanitized, "'" + payload, f"Failed to neutralize {payload!r}: got {sanitized!r}")

        # Literal "-" and empty values remain preserved as "-" without leading quote
        self.assertEqual(utils.sanitize_csv_cell("-"), "-")
        self.assertEqual(utils.sanitize_csv_cell(""), "-")
        self.assertEqual(utils.sanitize_csv_cell(None), "-")
        self.assertEqual(utils.sanitize_csv_cell('""'), '""')


class TestDatabaseSecurityAndCore(IsolatedCoreTestCase):
    def test_record_check_out_preconditions_and_duplicate_guard(self):
        database.register_user(201, "Siswa Satu", "SMKN 1", "200001", status="approved")
        database.register_user(202, "Siswa Izin", "SMKN 1", "200002", status="approved")

        # No check-in yet
        self.assertFalse(
            database.record_check_out(201, "2026-11-23", "17:05:00", -6.17, 106.82, 5.0, "out.jpg", "Kantor", "Aktif")
        )

        # Leave row (check_in_time='-') cannot be checked out
        database.record_leave_attendance(202, "2026-11-23", "Izin", "izin.jpg")
        self.assertFalse(
            database.record_check_out(202, "2026-11-23", "17:05:00", -6.17, 106.82, 5.0, "out.jpg", "Kantor", "Aktif")
        )

        # Valid check-in
        self.assertTrue(
            database.record_check_in(201, "2026-11-23", "07:50:00", -6.17, 106.82, 5.0, "in.jpg", "Kantor")
        )
        # Wrong date returns False
        self.assertFalse(
            database.record_check_out(201, "2026-11-24", "17:05:00", -6.17, 106.82, 5.0, "out.jpg", "Kantor", "Aktif")
        )
        # First valid checkout succeeds
        self.assertTrue(
            database.record_check_out(201, "2026-11-23", "17:05:00", -6.17, 106.82, 5.0, "out.jpg", "Kantor", "Aktif")
        )
        # Second checkout on same day fails
        self.assertFalse(
            database.record_check_out(201, "2026-11-23", "17:10:00", -6.17, 106.82, 5.0, "out2.jpg", "Kantor", "Aktif2")
        )

    def test_atomic_user_and_leave_status_updates(self):
        database.register_user(301, "Calon Siswa", "SMK 1", "300001", status="pending")
        self.assertTrue(database.update_user_status(301, "approved"))
        # Second concurrent admin attempt must fail and not flip to rejected
        self.assertFalse(database.update_user_status(301, "rejected"))
        self.assertEqual(database.get_user(301)["status"], "approved")

        req_id = database.create_leave_request(301, "2026-11-23", "Sakit", "Demam", "surat.jpg")
        self.assertGreater(req_id, 0)
        self.assertTrue(database.update_leave_request_status(req_id, "approved"))
        self.assertFalse(database.update_leave_request_status(req_id, "rejected"))
        self.assertEqual(database.get_leave_request(req_id)["status"], "approved")

    def test_register_user_does_not_overwrite_approved_or_admin_role(self):
        # Pending user can be re-registered/updated while still pending
        self.assertTrue(database.register_user(401, "Nama Awal", "SMK A", "400001", role="admin", status="pending"))
        self.assertTrue(database.register_user(401, "Nama Baru", "SMK B", "400002", role="student", status="approved"))
        u = database.get_user(401)
        self.assertEqual(u["full_name"], "Nama Baru")
        self.assertEqual(u["role"], "admin")
        self.assertEqual(u["status"], "approved")

        # Already approved user cannot be overwritten back to pending/rejected/student
        self.assertFalse(database.register_user(401, "Attacker", "Fake", "999999", role="student", status="pending"))
        u_after = database.get_user(401)
        self.assertEqual(u_after["full_name"], "Nama Baru")
        self.assertEqual(u_after["role"], "admin")
        self.assertEqual(u_after["status"], "approved")

    def test_record_day_off_and_leave_do_not_clobber_hadir(self):
        database.register_user(501, "Hadir User", "SMK 1", "500001", status="approved")
        database.record_check_in(501, "2026-11-23", "07:45:00", -6.17, 106.82, 5.0, "in.jpg", "Kantor")

        self.assertFalse(database.record_day_off(501, "2026-11-23"))
        self.assertFalse(database.record_leave_attendance(501, "2026-11-23", "Izin", "izin.jpg"))

        att = database.get_today_attendance(501, "2026-11-23")
        self.assertEqual(att["status"], "Hadir")
        self.assertEqual(att["check_in_time"], "07:45:00")


class TestSheetsSyncSecurity(IsolatedCoreTestCase):
    async def test_rejects_non_https_urls(self):
        for bad_url in (
            "http://script.google.com/macros/s/abc/exec",
            "ftp://example.com/hook",
            "javascript:alert(1)",
        ):
            with patch.object(config, "GOOGLE_SHEET_WEBHOOK_URL", bad_url):
                with patch("sheets_sync.httpx.AsyncClient") as mock_client:
                    await sheets_sync.sync_attendance_to_sheets(
                        "Ani", "SMK 1", "12345", "Masuk", "Kantor", "2026-11-23", "07:50:00", 5.0, ""
                    )
                    mock_client.assert_not_called()

    async def test_enforce_https_hook_blocks_redirect_downgrade(self):
        https_req = httpx.Request("GET", "https://script.googleusercontent.com/macros/echo")
        await sheets_sync._enforce_https_hook(https_req)

        http_req = httpx.Request("GET", "http://insecure.example.com/hook")
        with self.assertRaises(ValueError):
            await sheets_sync._enforce_https_hook(http_req)

        # End-to-end check with MockTransport simulating HTTPS -> HTTP 302 redirect downgrade
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "script.google.com":
                return httpx.Response(302, headers={"Location": "http://downgraded.example.com/leak"})
            return httpx.Response(200)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            transport=transport,
            follow_redirects=True,
            event_hooks={"request": [sheets_sync._enforce_https_hook]},
        ) as client:
            with self.assertRaises(ValueError):
                await client.post("https://script.google.com/macros/s/test/exec", json={"a": 1})

    async def test_max_photo_bytes_and_hmac_signature(self):
        oversized_photo = self.photos_dir / "big.jpg"
        oversized_photo.write_bytes(b"X" * 32)

        captured = {}

        class FakeResponse:
            status_code = 200
            text = '{"status":"success"}'

        class FakeAsyncClient:
            def __init__(self, **kwargs):
                captured["client_kwargs"] = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, json=None):
                captured["url"] = url
                captured["payload"] = json
                return FakeResponse()

        secret = "super-secret-key-32-chars-minimum"
        with (
            patch.object(config, "GOOGLE_SHEET_WEBHOOK_URL", "https://script.google.com/macros/s/abc/exec"),
            patch.object(config, "GOOGLE_SHEET_WEBHOOK_SECRET", secret),
            patch.object(sheets_sync, "MAX_PHOTO_BYTES", 16),
            patch("sheets_sync.httpx.AsyncClient", FakeAsyncClient),
        ):
            await sheets_sync.sync_attendance_to_sheets(
                full_name="Ani",
                school="SMKN 1",
                nisn="123456",
                att_type="Masuk",
                office_name="Kantor Pusat",
                date_str="2026-11-23",
                time_str="07:45:00",
                distance=10.5,
                photo_path=str(oversized_photo),
                activity="-",
            )

        payload = captured["payload"]
        self.assertEqual(payload["image_base64"], "")
        self.assertIn("timestamp", payload)
        self.assertIn("nonce", payload)
        self.assertIn("signature", payload)

        expected_canonical = (
            f"{payload['timestamp']}:{payload['nonce']}:2026-11-23:07:45:00:Ani:Masuk"
        )
        expected_sig = hmac.new(
            secret.encode("utf-8"),
            expected_canonical.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(payload["signature"], expected_sig)
        self.assertEqual(
            sheets_sync.build_webhook_signature(
                secret, payload["timestamp"], payload["nonce"], "2026-11-23", "07:45:00", "Ani", "Masuk"
            ),
            expected_sig,
        )

    async def test_empty_and_none_field_hmac_normalization(self):
        secret = "super-secret-key-32-chars-minimum"
        ts = "1700000000"
        nonce = "deadbeefcafebabe"

        sig_empty = sheets_sync.build_webhook_signature(
            secret, ts, nonce, "2026-11-23", "07:45:00", "", "Masuk"
        )
        sig_none = sheets_sync.build_webhook_signature(
            secret, ts, nonce, "2026-11-23", "07:45:00", None, "Masuk"  # type: ignore[arg-type]
        )
        expected_canonical = f"{ts}:{nonce}:2026-11-23:07:45:00::Masuk"
        expected_sig = hmac.new(
            secret.encode("utf-8"),
            expected_canonical.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(sig_empty, expected_sig)
        self.assertEqual(sig_none, expected_sig)

        sent_payloads = []

        class FakeResponse:
            status_code = 200
            text = '{"status":"success"}'

        class FakeAsyncClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, json=None):
                sent_payloads.append(json)
                return FakeResponse()

        with (
            patch.object(config, "GOOGLE_SHEET_WEBHOOK_URL", "https://script.google.com/macros/s/abc/exec"),
            patch.object(config, "GOOGLE_SHEET_WEBHOOK_SECRET", secret),
            patch("sheets_sync.httpx.AsyncClient", FakeAsyncClient),
        ):
            for name_input in ("", None):
                await sheets_sync.sync_attendance_to_sheets(
                    full_name=name_input,  # type: ignore[arg-type]
                    school="SMKN 1",
                    nisn="123456",
                    att_type="Masuk",
                    office_name="Kantor Pusat",
                    date_str="2026-11-23",
                    time_str="07:45:00",
                    distance=5.0,
                    photo_path="",
                )

        self.assertEqual(len(sent_payloads), 2)
        for payload in sent_payloads:
            self.assertEqual(payload["full_name"], "")
            canonical = (
                f"{payload['timestamp']}:{payload['nonce']}:"
                f"{payload['date']}:{payload['time']}:{payload['full_name']}:{payload['type']}"
            )
            expected = hmac.new(
                secret.encode("utf-8"),
                canonical.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            self.assertEqual(payload["signature"], expected)
            self.assertEqual(
                sheets_sync.build_webhook_signature(
                    secret,
                    payload["timestamp"],
                    payload["nonce"],
                    payload["date"],
                    payload["time"],
                    payload["full_name"],
                    payload["type"],
                ),
                payload["signature"],
            )


class TestMainHandlersSecurityGuards(IsolatedCoreTestCase):
    def _make_update(
        self,
        user_id: int = 101,
        chat_type: str = ChatType.PRIVATE,
        text: str = "",
        location=None,
        photos=None,
        forward_origin=None,
        via_bot=None,
    ):
        update = MagicMock()
        update.effective_user = SimpleNamespace(id=user_id, first_name="Tester")
        update.effective_chat = SimpleNamespace(id=user_id, type=chat_type)
        msg = MagicMock()
        msg.text = text
        msg.location = location
        msg.photo = photos
        msg.forward_origin = forward_origin
        msg.via_bot = via_bot
        msg.reply_text = AsyncMock()
        msg.reply_document = AsyncMock()
        update.message = msg
        return update

    def _make_context(self, user_data=None):
        ctx = MagicMock()
        ctx.user_data = {} if user_data is None else dict(user_data)
        ctx.bot = MagicMock()
        ctx.bot.send_message = AsyncMock()
        return ctx

    def test_httpx_and_httpcore_logger_levels_and_private_guard(self):
        self.assertGreaterEqual(logging.getLogger("httpx").level, logging.WARNING)
        self.assertGreaterEqual(logging.getLogger("httpcore").level, logging.WARNING)

        self.assertFalse(main._is_private_message(None))
        no_msg = MagicMock(effective_user=SimpleNamespace(id=1), effective_chat=SimpleNamespace(type=ChatType.PRIVATE), message=None)
        self.assertFalse(main._is_private_message(no_msg))
        grp = self._make_update(chat_type=ChatType.GROUP)
        self.assertFalse(main._is_private_message(grp))
        sgrp = self._make_update(chat_type=ChatType.SUPERGROUP)
        self.assertFalse(main._is_private_message(sgrp))
        priv = self._make_update(chat_type=ChatType.PRIVATE)
        self.assertTrue(main._is_private_message(priv))

    async def test_location_handlers_reject_forward_bot_and_invalid_coords(self):
        database.register_user(101, "Siswa", "SMK 1", "100001", status="approved")

        with patch.object(utils, "is_past_entry_cutoff", return_value=(False, "08:00", "07:45:00")):
            # Forwarded location
            ctx = self._make_context()
            upd_fwd = self._make_update(
                user_id=101,
                location=SimpleNamespace(latitude=-6.175392, longitude=106.827153),
                forward_origin=object(),
            )
            state = await main.checkin_location_received(upd_fwd, ctx)
            self.assertEqual(state, main.CHECKIN_LOC)
            self.assertNotIn("checkin_lat", ctx.user_data)

            # Bot-relayed location
            upd_bot = self._make_update(
                user_id=101,
                location=SimpleNamespace(latitude=-6.175392, longitude=106.827153),
                via_bot=object(),
            )
            state = await main.checkin_location_received(upd_bot, ctx)
            self.assertEqual(state, main.CHECKIN_LOC)
            self.assertNotIn("checkin_lat", ctx.user_data)

            # Out-of-range and NaN coords
            for lat, lon in ((91.0, 106.8), (-91.0, 106.8), (-6.17, 181.0), (float("nan"), 106.8), (-6.17, float("inf"))):
                upd_bad = self._make_update(user_id=101, location=SimpleNamespace(latitude=lat, longitude=lon))
                state = await main.checkin_location_received(upd_bad, ctx)
                self.assertEqual(state, main.CHECKIN_LOC)
                self.assertNotIn("checkin_lat", ctx.user_data)

        # Checkout location rejects forwarded, via_bot, and invalid coords too
        ctx_out = self._make_context()
        upd_out_fwd = self._make_update(
            user_id=101,
            location=SimpleNamespace(latitude=-6.175392, longitude=106.827153),
            forward_origin=object(),
        )
        self.assertEqual(await main.checkout_location_received(upd_out_fwd, ctx_out), main.CHECKOUT_LOC)
        self.assertNotIn("checkout_lat", ctx_out.user_data)

        upd_out_nan = self._make_update(user_id=101, location=SimpleNamespace(latitude=float("nan"), longitude=106.8))
        self.assertEqual(await main.checkout_location_received(upd_out_nan, ctx_out), main.CHECKOUT_LOC)

    async def test_checkin_photo_toctou_revalidation(self):
        database.register_user(101, "Siswa", "SMK 1", "100001", status="approved")

        photo_file = MagicMock()
        photo_file.download_to_drive = AsyncMock()
        photo_obj = MagicMock()
        photo_obj.get_file = AsyncMock(return_value=photo_file)

        valid_state = {
            "checkin_lat": -6.175392,
            "checkin_lon": 106.827153,
            "checkin_distance": 0.0,
            "checkin_office": "Kantor <Pusat> & Utama",
            "checkin_date": "2026-11-23",
        }

        # 1. Past entry cutoff at photo step
        ctx1 = self._make_context(valid_state)
        upd1 = self._make_update(user_id=101, photos=[photo_obj])
        with (
            patch.object(utils, "get_today_str", return_value="2026-11-23"),
            patch.object(utils, "get_current_time_str", return_value="08:05:00"),
            patch.object(utils, "is_past_entry_cutoff", return_value=(True, "08:00", "08:05:00")),
        ):
            res = await main.checkin_photo_received(upd1, ctx1)
        self.assertEqual(res, ConversationHandler.END)
        self.assertIsNone(database.get_today_attendance(101, "2026-11-23"))
        photo_file.download_to_drive.assert_not_awaited()

        # 2. Date mismatch (crossed midnight)
        ctx2 = self._make_context({**valid_state, "checkin_date": "2026-11-22"})
        upd2 = self._make_update(user_id=101, photos=[photo_obj])
        with (
            patch.object(utils, "get_today_str", return_value="2026-11-23"),
            patch.object(utils, "is_past_entry_cutoff", return_value=(False, "08:00", "07:50:00")),
        ):
            res = await main.checkin_photo_received(upd2, ctx2)
        self.assertEqual(res, ConversationHandler.END)
        self.assertIsNone(database.get_today_attendance(101, "2026-11-23"))

        # 3. Unapproved user
        database.register_user(102, "Pending User", "SMK 1", "100002", status="pending")
        ctx3 = self._make_context(valid_state)
        upd3 = self._make_update(user_id=102, photos=[photo_obj])
        res = await main.checkin_photo_received(upd3, ctx3)
        self.assertEqual(res, ConversationHandler.END)
        self.assertIsNone(database.get_today_attendance(102, utils.get_today_str()))

    async def test_checkout_activity_toctou_and_missing_user_data_guard(self):
        database.register_user(101, "Siswa", "SMK 1", "100001", status="approved")
        database.record_check_in(101, "2026-11-23", "07:50:00", -6.175392, 106.827153, 5.0, "in.jpg", "Kantor Pusat")

        # 1. Before checkout open for non-admin: deletes temp photo and does NOT record checkout
        temp_photo1 = self.photos_dir / "co1.jpg"
        temp_photo1.write_bytes(b"img")
        ctx1 = self._make_context({
            "checkout_lat": -6.175392,
            "checkout_lon": 106.827153,
            "checkout_distance": 5.0,
            "checkout_office": "Kantor Pusat",
            "checkout_photo_path": str(temp_photo1),
            "checkout_date": "2026-11-23",
        })
        upd1 = self._make_update(user_id=101, text="Kerja laporan")
        with (
            patch.object(utils, "get_today_str", return_value="2026-11-23"),
            patch.object(utils, "get_current_time_str", return_value="16:45:00"),
            patch.object(utils, "is_checkout_open", return_value=(False, "17:00", "16:45:00")),
        ):
            res = await main.checkout_activity_received(upd1, ctx1)
        self.assertEqual(res, ConversationHandler.END)
        self.assertFalse(temp_photo1.exists())
        self.assertIsNone(database.get_today_attendance(101, "2026-11-23")["check_out_time"])

        # 2. Date mismatch deletes temp photo and rejects
        temp_photo2 = self.photos_dir / "co2.jpg"
        temp_photo2.write_bytes(b"img")
        ctx2 = self._make_context({
            "checkout_lat": -6.175392,
            "checkout_lon": 106.827153,
            "checkout_distance": 5.0,
            "checkout_office": "Kantor Pusat",
            "checkout_photo_path": str(temp_photo2),
            "checkout_date": "2026-11-22",
        })
        upd2 = self._make_update(user_id=101, text="Kerja laporan")
        with (
            patch.object(utils, "get_today_str", return_value="2026-11-23"),
            patch.object(utils, "is_checkout_open", return_value=(True, "17:00", "17:10:00")),
        ):
            res = await main.checkout_activity_received(upd2, ctx2)
        self.assertEqual(res, ConversationHandler.END)
        self.assertFalse(temp_photo2.exists())
        self.assertIsNone(database.get_today_attendance(101, "2026-11-23")["check_out_time"])

        # 3. Missing user_data keys must NOT default to 0.0 and commit checkout
        temp_photo3 = self.photos_dir / "co3.jpg"
        temp_photo3.write_bytes(b"img")
        ctx3 = self._make_context({
            "checkout_photo_path": str(temp_photo3),
            "checkout_date": "2026-11-23",
        })
        upd3 = self._make_update(user_id=101, text="Kerja laporan")
        with (
            patch.object(utils, "get_today_str", return_value="2026-11-23"),
            patch.object(utils, "is_checkout_open", return_value=(True, "17:00", "17:10:00")),
        ):
            res = await main.checkout_activity_received(upd3, ctx3)
        self.assertEqual(res, ConversationHandler.END)
        self.assertFalse(temp_photo3.exists())
        att = database.get_today_attendance(101, "2026-11-23")
        self.assertIsNone(att["check_out_time"])
        self.assertIsNone(att["check_out_lat"])

    async def test_callback_handlers_auth_invalid_data_and_race(self):
        database.register_user(101, "Calon <Siswa>", "SMK 1", "100001", status="pending")
        req_id = database.create_leave_request(101, "2026-11-23", "Izin", "Urusan", "surat.jpg")

        def make_cb_update(user_id, data, chat_type=ChatType.PRIVATE):
            upd = MagicMock()
            upd.effective_user = SimpleNamespace(id=user_id, first_name="User")
            query = MagicMock()
            query.data = data
            query.message = MagicMock()
            query.message.chat = SimpleNamespace(type=chat_type)
            query.answer = AsyncMock()
            query.edit_message_text = AsyncMock()
            query.edit_message_caption = AsyncMock()
            upd.callback_query = query
            return upd

        ctx = self._make_context()

        # Non-admin rejected with alert before mutating DB
        cb_non_admin = make_cb_update(101, "approve_101")
        await main.admin_approval_callback(cb_non_admin, ctx)
        cb_non_admin.callback_query.answer.assert_awaited_once_with("⛔ Anda tidak memiliki hak akses admin.", show_alert=True)
        self.assertEqual(database.get_user(101)["status"], "pending")

        cb_leave_non_admin = make_cb_update(101, f"leaveapp_{req_id}")
        await main.leave_approval_callback(cb_leave_non_admin, ctx)
        cb_leave_non_admin.callback_query.answer.assert_awaited_once_with("⛔ Anda tidak memiliki hak akses admin.", show_alert=True)
        self.assertEqual(database.get_leave_request(req_id)["status"], "pending")

        # Non-private callback ignored completely
        cb_group = make_cb_update(9001, "approve_101", chat_type=ChatType.GROUP)
        await main.admin_approval_callback(cb_group, ctx)
        cb_group.callback_query.answer.assert_not_awaited()

        # Invalid callback data handled safely
        for bad_data in ("malformed", "unknown_101", "approve_notanint"):
            cb_bad = make_cb_update(9001, bad_data)
            await main.admin_approval_callback(cb_bad, ctx)
            cb_bad.callback_query.edit_message_text.assert_awaited_once()

        # First admin approves, second admin racing with reject sees already processed
        cb_admin1 = make_cb_update(9001, "approve_101")
        await main.admin_approval_callback(cb_admin1, ctx)
        self.assertEqual(database.get_user(101)["status"], "approved")

        cb_admin2 = make_cb_update(9002, "reject_101")
        await main.admin_approval_callback(cb_admin2, ctx)
        self.assertIn("sudah diproses", cb_admin2.callback_query.edit_message_text.await_args.args[0])
        self.assertEqual(database.get_user(101)["status"], "approved")

        # Same atomic race check on leave_approval_callback
        cb_l1 = make_cb_update(9001, f"leaveapp_{req_id}")
        await main.leave_approval_callback(cb_l1, ctx)
        self.assertEqual(database.get_leave_request(req_id)["status"], "approved")

        cb_l2 = make_cb_update(9002, f"leaverej_{req_id}")
        await main.leave_approval_callback(cb_l2, ctx)
        self.assertIn("sudah diproses", cb_l2.callback_query.edit_message_caption.await_args.kwargs["caption"])
        self.assertEqual(database.get_leave_request(req_id)["status"], "approved")

    async def test_profile_and_history_withhold_details_from_unapproved_users(self):
        ctx = self._make_context()

        # Unregistered user
        upd_unreg = self._make_update(user_id=777)
        await main.profile_handler(upd_unreg, ctx)
        self.assertNotIn("Kantor <Pusat>", upd_unreg.message.reply_text.await_args.args[0])
        await main.history_handler(upd_unreg, ctx)
        self.assertIn("belum terdaftar", upd_unreg.message.reply_text.await_args.args[0])

        # Pending user
        database.register_user(778, "Pending <Siswa>", "SMK 1", "778001", status="pending")
        upd_pend = self._make_update(user_id=778)
        await main.profile_handler(upd_pend, ctx)
        profile_reply = upd_pend.message.reply_text.await_args.args[0]
        self.assertNotIn("Kantor", profile_reply)
        self.assertNotIn("Daftar Kantor Terdaftar", profile_reply)
        self.assertIn("Pending &lt;Siswa&gt;", profile_reply)

        await main.history_handler(upd_pend, ctx)
        self.assertIn("belum disetujui", upd_pend.message.reply_text.await_args.args[0])

        # Approved user gets escaped office info & history
        database.register_user(779, "Approved <Siswa>", "SMK & Co", "779001", status="approved")
        database.record_check_in(779, "2026-11-23", "07:50:00", -6.17, 106.82, 5.0, "in.jpg", "Kantor <Pusat> & Utama")
        upd_app = self._make_update(user_id=779)
        await main.profile_handler(upd_app, ctx)
        app_profile = upd_app.message.reply_text.await_args.args[0]
        self.assertIn("Approved &lt;Siswa&gt;", app_profile)
        self.assertIn("Kantor &lt;Pusat&gt; &amp; Utama", app_profile)

        await main.history_handler(upd_app, ctx)
        hist_reply = upd_app.message.reply_text.await_args.args[0]
        self.assertIn("Kantor &lt;Pusat&gt; &amp; Utama", hist_reply)

    async def test_rekap_admin_handler_deletes_csv_in_finally(self):
        database.register_user(101, "Siswa", "SMK 1", "100001", status="approved")
        database.record_check_in(101, "2026-11-23", "07:50:00", -6.17, 106.82, 5.0, "in.jpg", "Kantor Pusat")

        ctx = self._make_context()
        upd_admin = self._make_update(user_id=9001)
        upd_admin.message.reply_document = AsyncMock(side_effect=RuntimeError("Telegram upload error"))

        with self.assertRaises(RuntimeError):
            await main.rekap_admin_handler(upd_admin, ctx)

        # Even on exception, generated CSV must have been deleted in finally:
        self.assertEqual(list(self.reports_dir.glob("*.csv")), [])


if __name__ == "__main__":
    unittest.main()
