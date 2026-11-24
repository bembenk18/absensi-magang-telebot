import json
import logging
import math
import os
from pathlib import Path
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent

# Muat environment variables dari file .env jika ada
load_dotenv(BASE_DIR / ".env")


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    val = raw.strip().lower()
    if val in ("true", "1", "yes", "on"):
        return True
    if val in ("false", "0", "no", "off"):
        return False
    logger.warning("Konfigurasi %s='%s' tidak valid; menggunakan default %s.", name, raw, default)
    return default


def _parse_int_env(name: str, default: int, min_val: int, max_val: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = int(raw.strip())
    except ValueError:
        logger.warning("Konfigurasi %s='%s' bukan angka; menggunakan default %d.", name, raw, default)
        return default
    if not (min_val <= val <= max_val):
        logger.warning(
            "Konfigurasi %s=%d di luar rentang [%d..%d]; menggunakan default %d.",
            name,
            val,
            min_val,
            max_val,
            default,
        )
        return default
    return val


def _parse_id_set(raw: str, env_name: str) -> set[int]:
    result: set[int] = set()
    for raw_id in raw.split(","):
        clean_id = raw_id.strip()
        if not clean_id:
            continue
        if clean_id.isdigit():
            result.add(int(clean_id))
        else:
            logger.warning("Mengabaikan ID non-numerik '%s' pada %s.", clean_id, env_name)
    return result


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TIMEZONE = os.getenv("TIMEZONE", "Asia/Jakarta").strip() or "Asia/Jakarta"

# Konfigurasi Sinkronisasi Google Sheets & Google Drive
GOOGLE_SHEET_WEBHOOK_URL = os.getenv("GOOGLE_SHEET_WEBHOOK_URL", "").strip()
GOOGLE_SHEET_WEBHOOK_SECRET = os.getenv("GOOGLE_SHEET_WEBHOOK_SECRET", "").strip()

# Jam Buka Absen Pulang (Default: 17:00 WIB / jam 5 sore)
CHECKOUT_OPEN_HOUR = _parse_int_env("CHECKOUT_OPEN_HOUR", 17, 0, 23)
CHECKOUT_OPEN_MINUTE = _parse_int_env("CHECKOUT_OPEN_MINUTE", 0, 0, 59)

# Admin IDs
ADMIN_IDS = _parse_id_set(os.getenv("ADMIN_IDS", ""), "ADMIN_IDS")
if not ADMIN_IDS:
    logger.warning("ADMIN_IDS kosong — tidak ada ID admin yang dikonfigurasi.")

# Konfigurasi Rekap Absensi Harian Otomatis (Setelah Jam Pulang)
RECAP_ENABLED = _parse_bool_env("RECAP_ENABLED", True)
RECAP_HOUR = _parse_int_env("RECAP_HOUR", 18, 0, 23)
RECAP_MINUTE = _parse_int_env("RECAP_MINUTE", 0, 0, 59)
RECAP_SKIP_WEEKENDS = _parse_bool_env("RECAP_SKIP_WEEKENDS", False)
RECAP_SEND_CSV = _parse_bool_env("RECAP_SEND_CSV", False)

_recap_recipients_parsed = _parse_id_set(os.getenv("RECAP_RECIPIENT_IDS", ""), "RECAP_RECIPIENT_IDS")
RECAP_RECIPIENT_IDS = _recap_recipients_parsed if _recap_recipients_parsed else set(ADMIN_IDS)

# Direktori penyimpanan data
DB_PATH = BASE_DIR / "attendance.db"
PHOTOS_DIR = BASE_DIR / "photos"
REPORTS_DIR = BASE_DIR / "reports"

PHOTOS_DIR.mkdir(exist_ok=True)
REPORTS_DIR.mkdir(exist_ok=True)

# ==========================================================
# PARSING DAFTAR KANTOR (Bisa 1, 2, atau lebih)
# ==========================================================
def _validate_office_entry(name: object, lat: object, lon: object, radius: object, source: str) -> dict | None:
    if isinstance(lat, bool) or isinstance(lon, bool) or isinstance(radius, bool):
        logger.warning("Konfigurasi %s bertipe boolean tidak valid; dilewati.", source)
        return None
    try:
        lat_f = float(lat)  # type: ignore[arg-type]
        lon_f = float(lon)  # type: ignore[arg-type]
        rad_f = float(radius)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        logger.warning("Konfigurasi %s tidak valid (%s); dilewati.", source, exc)
        return None
    if not (math.isfinite(lat_f) and math.isfinite(lon_f) and math.isfinite(rad_f)):
        logger.warning("Konfigurasi %s mengandung NaN/Inf; dilewati.", source)
        return None
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        logger.warning("Konfigurasi %s koordinat di luar batas geografis (%s, %s); dilewati.", source, lat_f, lon_f)
        return None
    if rad_f <= 0.0:
        logger.warning("Konfigurasi %s radius <= 0 (%s); dilewati.", source, rad_f)
        return None
    clean_name = str(name if name is not None else "Kantor").strip() or "Kantor"
    return {
        "name": clean_name,
        "lat": lat_f,
        "lon": lon_f,
        "radius": rad_f,
    }


OFFICES = []

# Opsi 1: Cek JSON string OFFICES
offices_json = os.getenv("OFFICES", "").strip()
if offices_json:
    try:
        parsed = json.loads(offices_json)
        if isinstance(parsed, list):
            for idx, item in enumerate(parsed):
                if not isinstance(item, dict):
                    logger.warning("Konfigurasi OFFICES[%d] bukan objek JSON; dilewati.", idx)
                    continue
                if "lat" not in item or "lon" not in item:
                    logger.warning("Konfigurasi OFFICES[%d] tidak memiliki lat/lon; dilewati.", idx)
                    continue
                validated = _validate_office_entry(
                    item.get("name", "Kantor"),
                    item.get("lat"),
                    item.get("lon"),
                    item.get("radius", 100),
                    f"OFFICES[{idx}]",
                )
                if validated is not None:
                    OFFICES.append(validated)
        else:
            logger.warning("Konfigurasi OFFICES harus berupa JSON array; diabaikan.")
    except Exception as exc:
        logger.warning("Gagal mem-parsing OFFICES JSON (%s); mencoba konfigurasi lain.", exc)

# Opsi 2: Cek format variabel bernomor (OFFICE_1_..., OFFICE_2_..., dst.)
if not OFFICES:
    i = 1
    while True:
        name = os.getenv(f"OFFICE_{i}_NAME")
        lat = os.getenv(f"OFFICE_{i}_LAT")
        lon = os.getenv(f"OFFICE_{i}_LON")
        radius = os.getenv(f"OFFICE_{i}_RADIUS")
        if lat and lon:
            validated = _validate_office_entry(
                name.strip() if name else f"Kantor {i}",
                lat,
                lon,
                radius if radius else os.getenv("MAX_DISTANCE_METERS", "100"),
                f"OFFICE_{i}",
            )
            if validated is not None:
                OFFICES.append(validated)
            i += 1
        else:
            break

# Opsi 3: Fallback ke single OFFICE_LATITUDE / OFFICE_LONGITUDE (kompatibilitas lama)
if not OFFICES:
    validated = _validate_office_entry(
        "Kantor Pusat",
        os.getenv("OFFICE_LATITUDE", "-6.175392"),
        os.getenv("OFFICE_LONGITUDE", "106.827153"),
        os.getenv("MAX_DISTANCE_METERS", "100"),
        "OFFICE_LATITUDE/LONGITUDE",
    )
    if validated is not None:
        OFFICES.append(validated)
    else:
        logger.warning("Menggunakan koordinat default Jakarta karena OFFICE_LATITUDE/LONGITUDE tidak valid.")
        OFFICES.append({
            "name": "Kantor Pusat",
            "lat": -6.175392,
            "lon": 106.827153,
            "radius": 100.0
        })
