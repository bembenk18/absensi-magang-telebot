import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional
import config
from utils import get_now

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = config.DB_PATH
DB_PATH = _DEFAULT_DB_PATH


def _resolve_db_path() -> Path:
    if DB_PATH != _DEFAULT_DB_PATH:
        return Path(DB_PATH)
    return Path(config.DB_PATH)


class _ManagedConnection:
    """Wrapper koneksi SQLite yang otomatis commit/rollback dan menutup koneksi."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def __enter__(self) -> sqlite3.Connection:
        return self._conn

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()
        return False


def get_connection() -> _ManagedConnection:
    conn = sqlite3.connect(_resolve_db_path(), timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
    except Exception:
        conn.close()
        raise
    return _ManagedConnection(conn)


def init_db():
    """Inisialisasi tabel database jika belum ada dan lakukan migrasi kolom baru."""
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("PRAGMA journal_mode = WAL")
        except sqlite3.Error as exc:
            logger.warning("Gagal mengaktifkan SQLite WAL mode: %s", exc)

        # Tabel User / Karyawan / Siswa
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                school TEXT,
                nisn TEXT,
                nik TEXT,
                role TEXT DEFAULT 'student',
                status TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
        """)

        # Tabel Absensi
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS attendances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                check_in_office TEXT,
                check_in_time TEXT,
                check_in_lat REAL,
                check_in_lon REAL,
                check_in_distance REAL,
                check_in_photo TEXT,
                check_out_office TEXT,
                check_out_time TEXT,
                check_out_lat REAL,
                check_out_lon REAL,
                check_out_distance REAL,
                check_out_photo TEXT,
                status TEXT DEFAULT 'Hadir',
                UNIQUE(telegram_id, date),
                FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
            )
        """)

        # Tabel Pengajuan Izin / Sakit
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS leave_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                leave_type TEXT NOT NULL,
                reason TEXT NOT NULL,
                photo_path TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
            )
        """)

        # Tabel Log Tugas Terjadwal (Idempotency Rekap Harian / Broadcast)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_job_logs (
                job_name TEXT NOT NULL,
                run_date TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                recipient_count INTEGER DEFAULT 0,
                PRIMARY KEY (job_name, run_date)
            )
        """)

        # Migrasi kolom users jika tabel sudah ada sebelumnya
        cursor.execute("PRAGMA table_info(users)")
        existing_user_cols = [row[1] for row in cursor.fetchall()]
        if "school" not in existing_user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN school TEXT")
        if "nisn" not in existing_user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN nisn TEXT")
        if "status" not in existing_user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'approved'")

        # Migrasi kolom attendances jika belum ada
        cursor.execute("PRAGMA table_info(attendances)")
        existing_att_cols = [row[1] for row in cursor.fetchall()]
        if "check_in_office" not in existing_att_cols:
            cursor.execute("ALTER TABLE attendances ADD COLUMN check_in_office TEXT")
        if "check_out_office" not in existing_att_cols:
            cursor.execute("ALTER TABLE attendances ADD COLUMN check_out_office TEXT")
        if "activity" not in existing_att_cols:
            cursor.execute("ALTER TABLE attendances ADD COLUMN activity TEXT")

        conn.commit()


def get_user(telegram_id: int) -> Optional[Dict[str, Any]]:
    """Ambil data user berdasarkan telegram_id."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def register_user(
    telegram_id: int,
    full_name: str,
    school: str,
    nisn: str,
    role: str = "student",
    status: str = "pending"
) -> bool:
    """
    Mendaftarkan user baru atau memperbarui pendaftaran yang masih pending/rejected.
    Tidak menimpa akun yang sudah berstatus 'approved' dan tidak mereset role admin.
    """
    now_str = get_now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO users (telegram_id, full_name, school, nisn, nik, role, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    full_name = excluded.full_name,
                    school = excluded.school,
                    nisn = excluded.nisn,
                    nik = excluded.nik,
                    role = CASE
                        WHEN users.role = 'admin' THEN 'admin'
                        ELSE users.role
                    END,
                    status = excluded.status
                WHERE users.status != 'approved'
            """, (telegram_id, full_name, school, nisn, nisn, role, status, now_str))
            conn.commit()
            return cursor.rowcount > 0
        except sqlite3.Error as exc:
            logger.error("Gagal mendaftarkan user %s: %s", telegram_id, exc)
            return False


def update_user_status(telegram_id: int, status: str) -> bool:
    """
    Memperbarui status persetujuan user ('approved' atau 'rejected') secara atomik
    hanya jika status saat ini masih 'pending'.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "UPDATE users SET status = ? WHERE telegram_id = ? AND status = 'pending'",
                (status, telegram_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        except sqlite3.Error as exc:
            logger.error("Gagal memperbarui status user %s: %s", telegram_id, exc)
            return False


def get_approved_students() -> List[Dict[str, Any]]:
    """Mengambil seluruh siswa yang berstatus 'approved' untuk broadcast/rekap."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE status = 'approved' AND role != 'admin'")
        return [dict(row) for row in cursor.fetchall()]


def get_today_attendance(telegram_id: int, date_str: str) -> Optional[Dict[str, Any]]:
    """Ambil catatan absensi user untuk tanggal tertentu."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM attendances
            WHERE telegram_id = ? AND date = ?
        """, (telegram_id, date_str))
        row = cursor.fetchone()
        return dict(row) if row else None


def record_check_in(
    telegram_id: int,
    date_str: str,
    time_str: str,
    lat: float,
    lon: float,
    distance: float,
    photo_path: str,
    office_name: str = "",
    status: str = "Hadir"
) -> bool:
    """Catat absensi masuk beserta nama kantor yang dituju."""
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO attendances (
                    telegram_id, date, check_in_office, check_in_time, check_in_lat, check_in_lon,
                    check_in_distance, check_in_photo, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (telegram_id, date_str, office_name, time_str, lat, lon, distance, photo_path, status))
            conn.commit()
            return cursor.rowcount > 0
        except sqlite3.Error as exc:
            logger.error("Gagal mencatat check-in user %s (%s): %s", telegram_id, date_str, exc)
            return False


def record_check_out(
    telegram_id: int,
    date_str: str,
    time_str: str,
    lat: float,
    lon: float,
    distance: float,
    photo_path: str,
    office_name: str = "",
    activity: str = ""
) -> bool:
    """
    Catat absensi pulang beserta nama kantor dan laporan kegiatan.
    Hanya berhasil jika terdapat catatan masuk valid dan belum pulang.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                UPDATE attendances
                SET check_out_office = ?,
                    check_out_time = ?,
                    check_out_lat = ?,
                    check_out_lon = ?,
                    check_out_distance = ?,
                    check_out_photo = ?,
                    activity = COALESCE(NULLIF(?, ''), activity)
                WHERE telegram_id = ?
                  AND date = ?
                  AND check_in_time IS NOT NULL
                  AND check_in_time != '-'
                  AND check_out_time IS NULL
            """, (office_name, time_str, lat, lon, distance, photo_path, activity, telegram_id, date_str))
            conn.commit()
            return cursor.rowcount > 0
        except sqlite3.Error as exc:
            logger.error("Gagal mencatat check-out user %s (%s): %s", telegram_id, date_str, exc)
            return False


def update_activity_report(telegram_id: int, date_str: str, activity: str) -> bool:
    """Memperbarui atau mengisi laporan kegiatan harian siswa."""
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                UPDATE attendances
                SET activity = ?
                WHERE telegram_id = ?
                  AND date = ?
                  AND check_in_time IS NOT NULL
                  AND check_in_time != '-'
            """, (activity, telegram_id, date_str))
            conn.commit()
            return cursor.rowcount > 0
        except sqlite3.Error as exc:
            logger.error("Gagal memperbarui laporan kegiatan user %s (%s): %s", telegram_id, date_str, exc)
            return False


def get_user_history(telegram_id: int, limit: int = 7) -> List[Dict[str, Any]]:
    """Ambil riwayat absensi seorang user."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM attendances
            WHERE telegram_id = ?
            ORDER BY date DESC
            LIMIT ?
        """, (telegram_id, limit))
        return [dict(row) for row in cursor.fetchall()]


def get_all_attendance(date_str: Optional[str] = None) -> List[Dict[str, Any]]:
    """Ambil seluruh data absensi digabung dengan data user untuk rekap."""
    with get_connection() as conn:
        cursor = conn.cursor()
        query = """
            SELECT a.*, u.full_name, u.school, u.nisn, u.role as user_role, u.status as user_status
            FROM attendances a
            JOIN users u ON a.telegram_id = u.telegram_id
        """
        params: list[Any] = []
        if date_str:
            query += " WHERE a.date = ?"
            params.append(date_str)
        query += " ORDER BY a.date DESC, a.check_in_time DESC"

        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def create_leave_request(telegram_id: int, date_str: str, leave_type: str, reason: str, photo_path: str) -> int:
    """Menyimpan pengajuan izin/sakit baru dan mengembalikan ID pengajuan (0 jika gagal)."""
    now_str = get_now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO leave_requests (telegram_id, date, leave_type, reason, photo_path, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """, (telegram_id, date_str, leave_type, reason, photo_path, now_str))
            conn.commit()
            return int(cursor.lastrowid or 0)
        except sqlite3.Error as exc:
            logger.error("Gagal membuat pengajuan izin user %s (%s): %s", telegram_id, date_str, exc)
            return 0


def get_leave_request(request_id: int) -> Optional[Dict[str, Any]]:
    """Mengambil data pengajuan izin/sakit beserta data siswa."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT l.*, u.full_name, u.school, u.nisn
            FROM leave_requests l
            LEFT JOIN users u ON l.telegram_id = u.telegram_id
            WHERE l.id = ?
        """, (request_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def update_leave_request_status(request_id: int, status: str) -> bool:
    """
    Memperbarui status pengajuan izin/sakit ('approved' / 'rejected') secara atomik
    hanya jika status saat ini masih 'pending'.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "UPDATE leave_requests SET status = ? WHERE id = ? AND status = 'pending'",
                (status, request_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        except sqlite3.Error as exc:
            logger.error("Gagal memperbarui status izin #%s: %s", request_id, exc)
            return False


def record_leave_attendance(telegram_id: int, date_str: str, leave_type: str, photo_path: str = "") -> bool:
    """Mencatat kehadiran sebagai Izin atau Sakit tanpa menimpa record Hadir yang sudah ada."""
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO attendances (
                    telegram_id, date, status, check_in_time, check_out_time, check_in_photo
                ) VALUES (?, ?, ?, '-', '-', ?)
                ON CONFLICT(telegram_id, date) DO UPDATE SET
                    status = excluded.status,
                    check_in_photo = excluded.check_in_photo
                WHERE attendances.status != 'Hadir'
                  AND (attendances.check_in_time IS NULL OR attendances.check_in_time = '-')
            """, (telegram_id, date_str, leave_type, photo_path))
            conn.commit()
            return cursor.rowcount > 0
        except sqlite3.Error as exc:
            logger.error("Gagal mencatat kehadiran izin user %s (%s): %s", telegram_id, date_str, exc)
            return False


def record_day_off(telegram_id: int, date_str: str) -> bool:
    """Mencatat kehadiran sebagai Libur tanpa menimpa record Hadir yang sudah ada."""
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO attendances (
                    telegram_id, date, status, check_in_time, check_out_time
                ) VALUES (?, ?, 'Libur', '-', '-')
                ON CONFLICT(telegram_id, date) DO UPDATE SET
                    status = 'Libur'
                WHERE attendances.status != 'Hadir'
                  AND (attendances.check_in_time IS NULL OR attendances.check_in_time = '-')
            """, (telegram_id, date_str))
            conn.commit()
            return cursor.rowcount > 0
        except sqlite3.Error as exc:
            logger.error("Gagal mencatat libur user %s (%s): %s", telegram_id, date_str, exc)
            return False


def has_scheduled_job_run(job_name: str, date_str: str) -> bool:
    """Mengecek apakah tugas terjadwal untuk tanggal tertentu sudah pernah dijalankan."""
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT 1 FROM scheduled_job_logs WHERE job_name = ? AND run_date = ? LIMIT 1",
                (job_name, date_str),
            )
            return cursor.fetchone() is not None
        except sqlite3.Error as exc:
            logger.error("Gagal mengecek scheduled_job_logs (%s, %s): %s", job_name, date_str, exc)
            return False


def mark_scheduled_job_run(job_name: str, date_str: str, sent_at: str, recipient_count: int = 0) -> bool:
    """Mencatat bahwa tugas terjadwal telah berhasil dijalankan pada tanggal tertentu."""
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT OR IGNORE INTO scheduled_job_logs (job_name, run_date, sent_at, recipient_count)
                VALUES (?, ?, ?, ?)
            """, (job_name, date_str, sent_at, recipient_count))
            conn.commit()
            return cursor.rowcount > 0
        except sqlite3.Error as exc:
            logger.error("Gagal mencatat scheduled_job_logs (%s, %s): %s", job_name, date_str, exc)
            return False


def get_daily_recap_summary(date_str: str) -> Dict[str, Any]:
    """Mengambil daftar siswa approved dan seluruh baris absensi pada tanggal tertentu."""
    return {
        "date": date_str,
        "students": get_approved_students(),
        "attendance_rows": get_all_attendance(date_str),
    }
