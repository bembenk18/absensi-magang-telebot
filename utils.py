import csv
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
import pytz
import config
from config import TIMEZONE

_DEFAULT_REPORTS_DIR = config.REPORTS_DIR
REPORTS_DIR = _DEFAULT_REPORTS_DIR

_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n", "\x0b", "\x0c")
_CSV_LEADING_WS_RE = re.compile(r"^[\s\u00a0\u200b\u200c\u200d\ufeff]+")
_CSV_QUOTED_FORMULA_RE = re.compile(r'^"+[\s\u00a0\u200b\u200c\u200d\ufeff]*[=+\-@]')


def _resolve_reports_dir() -> Path:
    if REPORTS_DIR != _DEFAULT_REPORTS_DIR:
        return Path(REPORTS_DIR)
    return Path(config.REPORTS_DIR)


def sanitize_csv_cell(value: Any) -> str:
    """Menetralkan formula injection pada sel CSV (awalan =, +, -, @, kontrol, spasi/zero-width)."""
    if value is None:
        return "-"
    text = str(value)
    if text == "" or text == "-":
        return "-"
    if text.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + text
    stripped = _CSV_LEADING_WS_RE.sub("", text)
    if stripped.startswith(("=", "+", "-", "@")) or _CSV_QUOTED_FORMULA_RE.match(stripped):
        return "'" + text
    return text


def get_now() -> datetime:
    """Mengembalikan datetime saat ini sesuai zona waktu yang dikonfigurasi."""
    tz = pytz.timezone(config.TIMEZONE or TIMEZONE)
    return datetime.now(tz)


def get_today_str() -> str:
    """Mengembalikan tanggal hari ini dalam format YYYY-MM-DD."""
    return get_now().strftime("%Y-%m-%d")


def get_current_time_str() -> str:
    """Mengembalikan waktu saat ini dalam format HH:MM:SS."""
    return get_now().strftime("%H:%M:%S")


def get_work_schedule(dt: datetime = None) -> dict:
    """
    Mengembalikan jadwal jam masuk kerja sesuai tanggal hari ini:
    - Tanggal 1 s/d 12: Jam masuk 08:00 WIB (Pengingat jam 07:00 WIB)
    - Tanggal 13 s/d akhir bulan: Jam masuk 09:00 WIB (Pengingat jam 08:00 WIB)
    """
    now = dt or get_now()
    day = now.day
    if 1 <= day <= 12:
        entry_h, entry_m = 8, 0
        rem_h, rem_m = 7, 0
        rule = "Tanggal 1 - 12 (Masuk: 08:00 WIB)"
    else:
        entry_h, entry_m = 9, 0
        rem_h, rem_m = 8, 0
        rule = "Tanggal 13 - akhir bulan (Masuk: 09:00 WIB)"

    return {
        "entry_hour": entry_h,
        "entry_minute": entry_m,
        "entry_time_str": f"{entry_h:02d}:{entry_m:02d}",
        "reminder_hour": rem_h,
        "reminder_minute": rem_m,
        "reminder_time_str": f"{rem_h:02d}:{rem_m:02d}",
        "rule": rule
    }


def is_past_entry_cutoff(dt: datetime = None) -> tuple[bool, str, str]:
    """
    Mengecek apakah waktu saat ini telah melewati batas jam masuk.
    Mengembalikan: (is_late, entry_time_str, current_time_str)
    """
    now = dt or get_now()
    sched = get_work_schedule(now)
    cutoff = now.replace(
        hour=sched["entry_hour"],
        minute=sched["entry_minute"],
        second=0,
        microsecond=0
    )
    is_late = now > cutoff
    return is_late, sched["entry_time_str"], now.strftime("%H:%M:%S")


def is_checkout_open(dt: datetime = None) -> tuple[bool, str, str]:
    """
    Mengecek apakah waktu saat ini telah memasuki jam buka absen pulang.
    Default: mulai pukul 17:00 WIB (jam 5 sore) hingga 23:59 WIB.
    Mengembalikan: (is_open, open_time_str, current_time_str)
    """
    now = dt or get_now()
    open_time = now.replace(
        hour=config.CHECKOUT_OPEN_HOUR,
        minute=config.CHECKOUT_OPEN_MINUTE,
        second=0,
        microsecond=0
    )
    is_open = now >= open_time
    open_time_str = f"{config.CHECKOUT_OPEN_HOUR:02d}:{config.CHECKOUT_OPEN_MINUTE:02d}"
    return is_open, open_time_str, now.strftime("%H:%M:%S")


def _parse_valid_coord(lat: Any, lon: Any) -> tuple[float, float] | None:
    if isinstance(lat, bool) or isinstance(lon, bool):
        return None
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat_f) and math.isfinite(lon_f)):
        return None
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        return None
    return lat_f, lon_f


def calculate_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Menghitung jarak antara dua koordinat (latitude, longitude) dalam satuan meter
    menggunakan formula Haversine. Melempar ValueError jika koordinat tidak finite atau di luar batas.
    """
    c1 = _parse_valid_coord(lat1, lon1)
    c2 = _parse_valid_coord(lat2, lon2)
    if c1 is None or c2 is None:
        raise ValueError("Koordinat harus berupa angka finite dalam rentang lat [-90..90], lon [-180..180].")
    lat1_f, lon1_f = c1
    lat2_f, lon2_f = c2

    R = 6371000.0  # Radius bumi dalam meter

    phi1 = math.radians(lat1_f)
    phi2 = math.radians(lat2_f)
    delta_phi = math.radians(lat2_f - lat1_f)
    delta_lambda = math.radians(lon2_f - lon1_f)

    a = (math.sin(delta_phi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2.0) ** 2))
    a = min(1.0, max(0.0, a))
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

    distance = R * c
    return round(distance, 2)


def check_proximity_all_offices(lat: float, lon: float, offices: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Mengecek lokasi user terhadap daftar semua kantor yang terdaftar.
    Fail-closed jika koordinat user/kantor tidak finite atau di luar batas, atau radius <= 0.
    """
    user_coords = _parse_valid_coord(lat, lon)
    if user_coords is None:
        return {
            "is_valid": False,
            "matched_office": None,
            "closest_office": None,
            "nearest_office": None,
            "distance": float("inf"),
            "min_distance": float("inf"),
            "details": [],
        }
    user_lat, user_lon = user_coords

    details = []
    matched = None
    min_dist = float("inf")

    for office in offices or []:
        if not isinstance(office, dict):
            continue
        office_coords = _parse_valid_coord(office.get("lat"), office.get("lon"))
        if office_coords is None:
            continue
        off_lat, off_lon = office_coords

        raw_radius = office.get("radius", 100.0)
        if isinstance(raw_radius, bool):
            continue
        try:
            radius = float(raw_radius)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(radius) and radius > 0.0):
            continue

        dist = calculate_distance(user_lat, user_lon, off_lat, off_lon)
        is_inside = dist <= radius

        info = {
            "name": office.get("name", "Kantor"),
            "distance": dist,
            "radius": radius,
            "is_inside": is_inside
        }
        details.append(info)

        if is_inside and dist < min_dist:
            min_dist = dist
            matched = info

    if matched:
        return {
            "is_valid": True,
            "matched_office": matched,
            "nearest_office": matched,
            "distance": matched["distance"],
            "min_distance": matched["distance"],
            "details": details
        }
    else:
        closest = min(details, key=lambda x: x["distance"]) if details else None
        closest_dist = closest["distance"] if closest else float("inf")
        return {
            "is_valid": False,
            "matched_office": None,
            "closest_office": closest,
            "nearest_office": closest,
            "min_distance": closest_dist,
            "details": details
        }


def generate_csv_report(records: list[dict], filename_prefix: str = "rekap_absensi") -> str:
    """
    Membuat file CSV dari list data absensi dengan proteksi formula injection
    dan mengembalikan path filenya.
    """
    reports_dir = _resolve_reports_dir()
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = get_now().strftime("%Y%m%d_%H%M%S")
    filepath = reports_dir / f"{filename_prefix}_{timestamp}.csv"
    counter = 1
    while filepath.exists():
        filepath = reports_dir / f"{filename_prefix}_{timestamp}_{counter}.csv"
        counter += 1

    fieldnames = [
        "Tanggal",
        "Nama Lengkap",
        "Asal Sekolah",
        "NISN",
        "Kantor Masuk",
        "Jam Masuk",
        "Jarak Masuk (m)",
        "Kantor Pulang",
        "Jam Pulang",
        "Jarak Pulang (m)",
        "Laporan Kegiatan",
        "Status"
    ]

    with open(filepath, mode="w", newline="", encoding="utf-8-sig") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for r in records:
            in_dist = r.get("check_in_distance")
            out_dist = r.get("check_out_distance")
            writer.writerow({
                "Tanggal": sanitize_csv_cell(r.get("date", "-")),
                "Nama Lengkap": sanitize_csv_cell(r.get("full_name", "-")),
                "Asal Sekolah": sanitize_csv_cell(r.get("school") or "-"),
                "NISN": sanitize_csv_cell(r.get("nisn") or r.get("nik") or "-"),
                "Kantor Masuk": sanitize_csv_cell(r.get("check_in_office") or "-"),
                "Jam Masuk": sanitize_csv_cell(r.get("check_in_time") or "-"),
                "Jarak Masuk (m)": sanitize_csv_cell(in_dist if in_dist is not None else "-"),
                "Kantor Pulang": sanitize_csv_cell(r.get("check_out_office") or "-"),
                "Jam Pulang": sanitize_csv_cell(r.get("check_out_time") or "-"),
                "Jarak Pulang (m)": sanitize_csv_cell(out_dist if out_dist is not None else "-"),
                "Laporan Kegiatan": sanitize_csv_cell(r.get("activity") or "-"),
                "Status": sanitize_csv_cell(r.get("status", "Hadir"))
            })

    return str(filepath)
