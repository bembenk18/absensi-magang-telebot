import base64
import hashlib
import hmac
import json
import logging
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
from aiohttp import web

import config
import database
import utils
import sheets_sync

logger = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).resolve().parent / "webapp"

def validate_telegram_init_data(init_data: str, bot_token: str) -> dict | None:
    """Memvalidasi integritas data Telegram WebApp menggunakan HMAC-SHA256."""
    if not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        if "hash" not in parsed:
            return None
        received_hash = parsed.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if calculated_hash == received_hash:
            user_str = parsed.get("user")
            return json.loads(user_str) if user_str else {}
        return None
    except Exception as e:
        logger.warning("Gagal validasi initData: %s", e)
        return None

async def handle_index(request: web.Request) -> web.Response:
    """Menyajikan halaman HTML kamera langsung Telegram Mini App."""
    index_file = WEBAPP_DIR / "index.html"
    if not index_file.exists():
        return web.Response(text="Web app index file not found", status=404)
    return web.FileResponse(index_file)

async def handle_check_location(request: web.Request) -> web.Response:
    """Mengecek jarak koordinat user ke semua kantor yang terdaftar."""
    try:
        lat = float(request.query.get("lat", 0))
        lon = float(request.query.get("lon", 0))
    except ValueError:
        return web.json_response({"is_valid": False, "message": "Koordinat tidak valid"}, status=400)

    res = utils.check_proximity_all_offices(lat, lon, config.OFFICES)
    return web.json_response(res)

async def handle_submit_attendance(request: web.Request) -> web.Response:
    """Menerima dan memproses absensi langsung dari kamera Telegram Mini App."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"success": False, "message": "Payload JSON tidak valid"}, status=400)

    init_data = body.get("initData", "")
    type_str = body.get("type", "checkin")
    lat = float(body.get("lat", 0.0))
    lon = float(body.get("lon", 0.0))
    image_data = body.get("image", "")

    # Validasi User
    verified_user = validate_telegram_init_data(init_data, config.BOT_TOKEN)
    if verified_user:
        user_id = verified_user.get("id")
        user_name = f"{verified_user.get('first_name', '')} {verified_user.get('last_name', '')}".strip() or "Karyawan"
    else:
        user_id = int(body.get("user_id", 0))
        user_name = body.get("full_name", "Karyawan")

    if not user_id:
        return web.json_response({"success": False, "message": "Identitas pengguna tidak ditemukan."}, status=401)

    # 1. Validasi Proximity Kantor
    proximity = utils.check_proximity_all_offices(lat, lon, config.OFFICES)
    if not proximity["is_valid"]:
        return web.json_response({
            "success": False,
            "message": "Anda berada di luar radius kantor Blimbing & Juanda!"
        }, status=400)

    matched_office = proximity["matched_office"]
    office_name = matched_office["name"]
    distance = proximity["distance"]

    # 2. Cek status di database
    today_str = utils.get_today_str()
    time_str = utils.get_current_time_str()
    db_user = database.get_user(user_id)
    if not db_user:
        return web.json_response({
            "success": False,
            "message": "Anda belum terdaftar. Silakan kirim /start di chat bot untuk mendaftar."
        }, status=403)

    user_status = db_user.get("status", "pending")
    if user_status == "pending":
        return web.json_response({
            "success": False,
            "message": "Pendaftaran Anda sedang menunggu persetujuan (approval) dari Admin."
        }, status=403)
    elif user_status == "rejected":
        return web.json_response({
            "success": False,
            "message": "Pendaftaran Anda telah ditolak oleh Admin."
        }, status=403)

    today_att = database.get_today_attendance(user_id, today_str)

    if type_str == "checkin":
        # Validasi batas maksimal jam masuk
        is_late, entry_time_str, cur_time = utils.is_past_entry_cutoff()
        if is_late:
            return web.json_response({
                "success": False,
                "message": f"Absen Masuk Ditolak! Batas maksimal jam masuk ({entry_time_str} WIB) telah lewat (sekarang {cur_time} WIB). Silakan segera hubungi pembimbing!"
            }, status=403)

        if today_att and today_att.get("check_in_time"):
            return web.json_response({
                "success": False,
                "message": f"Anda sudah absen masuk hari ini pukul {today_att['check_in_time']} WIB!"
            }, status=400)
    elif type_str == "checkout":
        if not today_att or not today_att.get("check_in_time"):
            return web.json_response({
                "success": False,
                "message": "Anda belum melakukan absen masuk hari ini!"
            }, status=400)
        if today_att.get("check_out_time"):
            return web.json_response({
                "success": False,
                "message": f"Anda sudah absen pulang hari ini pukul {today_att['check_out_time']} WIB!"
            }, status=400)

    # 3. Simpan Foto Kamera Langsung
    if not image_data or "," not in image_data:
        return web.json_response({"success": False, "message": "Foto selfie tidak valid."}, status=400)

    header, encoded = image_data.split(",", 1)
    photo_bytes = base64.b64decode(encoded)

    filename = f"live_{type_str}_{user_id}_{today_str}_{time_str.replace(':', '')}.jpg"
    filepath = config.PHOTOS_DIR / filename
    with open(filepath, "wb") as f:
        f.write(photo_bytes)

    # 4. Simpan ke Database
    if type_str == "checkin":
        success = database.record_check_in(
            telegram_id=user_id,
            date_str=today_str,
            time_str=time_str,
            lat=lat,
            lon=lon,
            distance=distance,
            photo_path=str(filepath),
            office_name=office_name,
            status="Hadir (Live Cam)"
        )
    else:
        success = database.record_check_out(
            telegram_id=user_id,
            date_str=today_str,
            time_str=time_str,
            lat=lat,
            lon=lon,
            distance=distance,
            photo_path=str(filepath),
            office_name=office_name
        )

    if not success:
        return web.json_response({"success": False, "message": "Gagal mencatat data ke database."}, status=500)

    # 5. Sinkronisasi Otomatis ke Google Sheets & Google Drive
    sheets_sync.trigger_sheet_sync(
        full_name=db_user.get("full_name", user_name),
        school=db_user.get("school", "-"),
        nisn=db_user.get("nisn", "-"),
        att_type="Masuk" if type_str == "checkin" else "Pulang",
        office_name=office_name,
        date_str=today_str,
        time_str=time_str,
        distance=distance,
        photo_path=str(filepath)
    )

    # 6. Kirim Bukti Struk Absensi ke Chat Telegram Karyawan
    asyncio_task = send_telegram_receipt(user_id, type_str, office_name, today_str, time_str, distance, str(filepath))
    try:
        await asyncio_task
    except Exception as e:
        logger.warning("Gagal mengirim notif telegram: %s", e)

    return web.json_response({
        "success": True,
        "office": office_name,
        "distance": distance,
        "time": time_str
    })

async def send_telegram_receipt(user_id: int, type_str: str, office_name: str, date_str: str, time_str: str, distance: float, photo_path: str):
    """Mengirim pesan konfirmasi langsung ke chat Telegram karyawan via Bot API."""
    if not config.BOT_TOKEN:
        return
    
    label = "Absen Masuk" if type_str == "checkin" else "Absen Pulang"
    caption = (
        f"🎉 *{label} Berhasil via Kamera Langsung!*\n\n"
        f"🏢 Lokasi: *{office_name}*\n"
        f"📅 Tanggal: *{date_str}*\n"
        f"🕒 Waktu: *{time_str} WIB*\n"
        f"📍 Jarak: *{distance:,.0f} meter*\n\n"
        f"🔒 _Terverifikasi: Foto diambil langsung dari kamera._"
    )

    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendPhoto"
    async with httpx.AsyncClient(timeout=10.0) as client:
        with open(photo_path, "rb") as f:
            files = {"photo": f}
            data = {"chat_id": user_id, "caption": caption, "parse_mode": "Markdown"}
            await client.post(url, data=data, files=files)

def create_webapp():
    """Membuat aplikasi aiohttp untuk Telegram Mini App."""
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/check-location", handle_check_location)
    app.router.add_post("/api/submit-attendance", handle_submit_attendance)
    return app
