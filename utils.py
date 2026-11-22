import math
import csv
from datetime import datetime
from typing import List, Dict, Any
import pytz
import config
from config import TIMEZONE, REPORTS_DIR

def get_now() -> datetime:
    """Mengembalikan datetime saat ini sesuai zona waktu yang dikonfigurasi."""
    tz = pytz.timezone(TIMEZONE)
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

def calculate_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Menghitung jarak antara dua koordinat (latitude, longitude) dalam satuan meter
    menggunakan formula Haversine.
    """
    R = 6371000.0  # Radius bumi dalam meter

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (math.sin(delta_phi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2.0) ** 2))
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

    distance = R * c
    return round(distance, 2)

def check_proximity_all_offices(lat: float, lon: float, offices: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Mengecek lokasi user terhadap daftar semua kantor yang terdaftar.
    Jika user berada dalam radius salah satu kantor, maka absensi valid di kantor tersebut.
    """
    details = []
    matched = None
    min_dist = float("inf")

    for office in offices:
        dist = calculate_distance(lat, lon, office["lat"], office["lon"])
        radius = office.get("radius", 100.0)
        is_inside = (radius <= 0) or (dist <= radius)
        
        info = {
            "name": office["name"],
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
            "distance": matched["distance"],
            "details": details
        }
    else:
        closest = min(details, key=lambda x: x["distance"]) if details else None
        return {
            "is_valid": False,
            "matched_office": None,
            "closest_office": closest,
            "details": details
        }

def generate_csv_report(records: list[dict], filename_prefix: str = "rekap_absensi") -> str:
    """
    Membuat file CSV dari list data absensi dan mengembalikan path filenya.
    """
    timestamp = get_now().strftime("%Y%m%d_%H%M%S")
    filepath = REPORTS_DIR / f"{filename_prefix}_{timestamp}.csv"

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
            writer.writerow({
                "Tanggal": r.get("date", "-"),
                "Nama Lengkap": r.get("full_name", "-"),
                "Asal Sekolah": r.get("school") or "-",
                "NISN": r.get("nisn") or r.get("nik") or "-",
                "Kantor Masuk": r.get("check_in_office") or "-",
                "Jam Masuk": r.get("check_in_time") or "-",
                "Jarak Masuk (m)": r.get("check_in_distance") if r.get("check_in_distance") is not None else "-",
                "Kantor Pulang": r.get("check_out_office") or "-",
                "Jam Pulang": r.get("check_out_time") or "-",
                "Jarak Pulang (m)": r.get("check_out_distance") if r.get("check_out_distance") is not None else "-",
                "Laporan Kegiatan": r.get("activity") or "-",
                "Status": r.get("status", "Hadir")
            })

    return str(filepath)
