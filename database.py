import sqlite3
from typing import Optional, List, Dict, Any
from config import DB_PATH
from utils import get_now

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Inisialisasi tabel database jika belum ada dan lakukan migrasi kolom baru."""
    with get_connection() as conn:
        cursor = conn.cursor()
        
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
    """Mendaftarkan user baru dengan status awal."""
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
                    role = excluded.role,
                    status = excluded.status
            """, (telegram_id, full_name, school, nisn, nisn, role, status, now_str))
            conn.commit()
            return True
        except sqlite3.Error:
            return False

def update_user_status(telegram_id: int, status: str) -> bool:
    """Memperbarui status persetujuan user ('approved' atau 'rejected')."""
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("UPDATE users SET status = ? WHERE telegram_id = ?", (status, telegram_id))
            conn.commit()
            return True
        except sqlite3.Error:
            return False

def get_approved_students() -> List[Dict[str, Any]]:
    """Mengambil seluruh siswa yang berstatus 'approved' untuk keperluan broadcast pengingat."""
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
            return True
        except sqlite3.Error:
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
    """Catat absensi pulang beserta nama kantor dan laporan kegiatan."""
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
                WHERE telegram_id = ? AND date = ?
            """, (office_name, time_str, lat, lon, distance, photo_path, activity, telegram_id, date_str))
            conn.commit()
            return True
        except sqlite3.Error:
            return False

def update_activity_report(telegram_id: int, date_str: str, activity: str) -> bool:
    """Memperbarui atau mengisi laporan kegiatan harian siswa."""
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                UPDATE attendances 
                SET activity = ?
                WHERE telegram_id = ? AND date = ?
            """, (activity, telegram_id, date_str))
            conn.commit()
            return cursor.rowcount > 0
        except sqlite3.Error:
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
            SELECT a.*, u.full_name, u.school, u.nisn, u.status as user_status 
            FROM attendances a
            JOIN users u ON a.telegram_id = u.telegram_id
        """
        params = []
        if date_str:
            query += " WHERE a.date = ?"
            params.append(date_str)
        query += " ORDER BY a.date DESC, a.check_in_time DESC"
        
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

def create_leave_request(telegram_id: int, date_str: str, leave_type: str, reason: str, photo_path: str) -> int:
    """Menyimpan pengajuan izin/sakit baru dan mengembalikan ID pengajuan."""
    now_str = get_now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO leave_requests (telegram_id, date, leave_type, reason, photo_path, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """, (telegram_id, date_str, leave_type, reason, photo_path, now_str))
        conn.commit()
        return cursor.lastrowid

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
    """Memperbarui status pengajuan izin/sakit ('approved' / 'rejected')."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE leave_requests SET status = ? WHERE id = ?", (status, request_id))
        conn.commit()
        return cursor.rowcount > 0

def record_leave_attendance(telegram_id: int, date_str: str, leave_type: str, photo_path: str = "") -> bool:
    """Mencatat kehadiran sebagai Izin atau Sakit di tabel attendances."""
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
            """, (telegram_id, date_str, leave_type, photo_path))
            conn.commit()
            return True
        except sqlite3.Error:
            return False

def record_day_off(telegram_id: int, date_str: str) -> bool:
    """Mencatat kehadiran sebagai Libur (pada hari Sabtu/Minggu)."""
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO attendances (
                    telegram_id, date, status, check_in_time, check_out_time
                ) VALUES (?, ?, 'Libur', '-', '-')
                ON CONFLICT(telegram_id, date) DO UPDATE SET
                    status = 'Libur'
            """, (telegram_id, date_str))
            conn.commit()
            return True
        except sqlite3.Error:
            return False
