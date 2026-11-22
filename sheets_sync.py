import asyncio
import base64
import logging
from pathlib import Path
from typing import Optional
import httpx
import config

logger = logging.getLogger(__name__)

async def sync_attendance_to_sheets(
    full_name: str,
    school: str,
    nisn: str,
    att_type: str,
    office_name: str,
    date_str: str,
    time_str: str,
    distance: float,
    photo_path: str
):
    """
    Mengirimkan data absensi beserta foto selfie ke Google Apps Script Webhook
    secara otomatis untuk dicatat di Google Sheets dan diunggah ke Google Drive.
    """
    webhook_url = config.GOOGLE_SHEET_WEBHOOK_URL
    if not webhook_url:
        return

    try:
        image_b64 = ""
        p = Path(photo_path)
        if p.exists():
            with open(p, "rb") as f:
                image_b64 = base64.b64encode(f.read()).decode("utf-8")

        payload = {
            "date": date_str,
            "time": time_str,
            "full_name": full_name,
            "school": school,
            "nisn": nisn,
            "type": att_type,
            "office_name": office_name,
            "distance": distance,
            "filename": p.name if p.exists() else f"selfie_{date_str}_{time_str.replace(':', '')}.jpg",
            "image_base64": image_b64
        }

        # Google Apps Script Webhook melakukan redirect HTTP 302, sehingga follow_redirects=True wajib
        async with httpx.AsyncClient(timeout=35.0, follow_redirects=True) as client:
            response = await client.post(webhook_url, json=payload)
            if response.status_code == 200:
                logger.info("✅ Berhasil menyinkronkan absensi %s ke Google Sheets & Drive.", full_name)
            else:
                logger.warning("⚠️ Google Sheets Webhook merespons status %d: %s", response.status_code, response.text)
    except Exception as e:
        logger.error("❌ Gagal menyinkronkan ke Google Sheets: %s", e)

def trigger_sheet_sync(
    full_name: str,
    school: str,
    nisn: str,
    att_type: str,
    office_name: str,
    date_str: str,
    time_str: str,
    distance: float,
    photo_path: str
):
    """Menjalankan sinkronisasi secara non-blocking di background tanpa memperlambat bot."""
    if config.GOOGLE_SHEET_WEBHOOK_URL:
        asyncio.create_task(
            sync_attendance_to_sheets(
                full_name=full_name,
                school=school,
                nisn=nisn,
                att_type=att_type,
                office_name=office_name,
                date_str=date_str,
                time_str=time_str,
                distance=distance,
                photo_path=photo_path
            )
        )
