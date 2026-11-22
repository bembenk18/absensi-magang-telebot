import json
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# Muat environment variables dari file .env jika ada
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TIMEZONE = os.getenv("TIMEZONE", "Asia/Jakarta")

# Konfigurasi Telegram Mini App (Web App)
WEB_APP_URL = os.getenv("WEB_APP_URL", "").strip()
try:
    WEB_APP_PORT = int(os.getenv("WEB_APP_PORT", "8080"))
except ValueError:
    WEB_APP_PORT = 8080

# Konfigurasi Sinkronisasi Google Sheets & Google Drive
GOOGLE_SHEET_WEBHOOK_URL = os.getenv("GOOGLE_SHEET_WEBHOOK_URL", "").strip()

# Admin IDs
admin_ids_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = set()
for raw_id in admin_ids_raw.split(","):
    clean_id = raw_id.strip()
    if clean_id.isdigit():
        ADMIN_IDS.add(int(clean_id))

# Direktori penyimpanan data
DB_PATH = BASE_DIR / "attendance.db"
PHOTOS_DIR = BASE_DIR / "photos"
REPORTS_DIR = BASE_DIR / "reports"

PHOTOS_DIR.mkdir(exist_ok=True)
REPORTS_DIR.mkdir(exist_ok=True)

# ==========================================================
# PARSING DAFTAR KANTOR (Bisa 1, 2, atau lebih)
# ==========================================================
OFFICES = []

# Opsi 1: Cek JSON string OFFICES
offices_json = os.getenv("OFFICES", "").strip()
if offices_json:
    try:
        parsed = json.loads(offices_json)
        if isinstance(parsed, list):
            for item in parsed:
                OFFICES.append({
                    "name": item.get("name", "Kantor"),
                    "lat": float(item["lat"]),
                    "lon": float(item["lon"]),
                    "radius": float(item.get("radius", 100))
                })
    except Exception:
        pass

# Opsi 2: Cek format variabel bernomor (OFFICE_1_..., OFFICE_2_..., dst.)
if not OFFICES:
    i = 1
    while True:
        name = os.getenv(f"OFFICE_{i}_NAME")
        lat = os.getenv(f"OFFICE_{i}_LAT")
        lon = os.getenv(f"OFFICE_{i}_LON")
        radius = os.getenv(f"OFFICE_{i}_RADIUS")
        if lat and lon:
            try:
                OFFICES.append({
                    "name": name.strip() if name else f"Kantor {i}",
                    "lat": float(lat),
                    "lon": float(lon),
                    "radius": float(radius) if radius else float(os.getenv("MAX_DISTANCE_METERS", "100"))
                })
            except ValueError:
                pass
            i += 1
        else:
            break

# Opsi 3: Fallback ke single OFFICE_LATITUDE / OFFICE_LONGITUDE (kompatibilitas lama)
if not OFFICES:
    try:
        lat = float(os.getenv("OFFICE_LATITUDE", "-6.175392"))
        lon = float(os.getenv("OFFICE_LONGITUDE", "106.827153"))
        radius = float(os.getenv("MAX_DISTANCE_METERS", "100"))
        OFFICES.append({
            "name": "Kantor Pusat",
            "lat": lat,
            "lon": lon,
            "radius": radius
        })
    except ValueError:
        OFFICES.append({
            "name": "Kantor Pusat",
            "lat": -6.175392,
            "lon": 106.827153,
            "radius": 100.0
        })
