import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
import time
from pathlib import Path
from typing import Set
import httpx
import config

logger = logging.getLogger(__name__)

MAX_PHOTO_BYTES = 10 * 1024 * 1024  # 10 MB
_background_tasks: Set[asyncio.Task] = set()


async def _enforce_https_hook(request: httpx.Request) -> None:
    """Menolak request atau redirect yang turun ke protokol non-HTTPS."""
    if request.url.scheme.lower() != "https":
        raise ValueError(f"Blocked non-HTTPS webhook or redirect scheme: {request.url.scheme}")


def _norm_sig_field(value: object) -> str:
    return "" if value is None else str(value)


def build_webhook_signature(
    secret: str,
    timestamp: str,
    nonce: str,
    date_str: str,
    time_str: str,
    full_name: str,
    att_type: str,
) -> str:
    """Membuat tanda tangan HMAC-SHA256 kanonikal untuk autentikasi Google Apps Script."""
    canonical = (
        f"{_norm_sig_field(timestamp)}:{_norm_sig_field(nonce)}:"
        f"{_norm_sig_field(date_str)}:{_norm_sig_field(time_str)}:"
        f"{_norm_sig_field(full_name)}:{_norm_sig_field(att_type)}"
    )
    return hmac.new(
        secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


async def sync_attendance_to_sheets(
    full_name: str,
    school: str,
    nisn: str,
    att_type: str,
    office_name: str,
    date_str: str,
    time_str: str,
    distance: float,
    photo_path: str,
    activity: str = "-",
):
    """
    Mengirimkan data absensi beserta foto selfie dan laporan kegiatan ke Google Apps Script Webhook
    secara aman (HTTPS-only + HMAC-SHA256) untuk dicatat di Google Sheets dan Google Drive.
    """
    webhook_url = (config.GOOGLE_SHEET_WEBHOOK_URL or "").strip()
    if not webhook_url:
        return

    if not webhook_url.lower().startswith("https://"):
        logger.error("❌ GOOGLE_SHEET_WEBHOOK_URL ditolak karena bukan HTTPS.")
        return

    try:
        image_b64 = ""
        filename = "-"
        if photo_path:
            p = Path(photo_path)
            if p.is_file():
                file_size = p.stat().st_size
                if file_size > MAX_PHOTO_BYTES:
                    logger.warning(
                        "⚠️ Ukuran foto %s (%d bytes) melebihi batas %d bytes; foto tidak dikirim ke webhook.",
                        p.name,
                        file_size,
                        MAX_PHOTO_BYTES,
                    )
                else:
                    filename = p.name
                    with open(p, "rb") as f:
                        image_b64 = base64.b64encode(f.read()).decode("utf-8")

        date_val = _norm_sig_field(date_str)
        time_val = _norm_sig_field(time_str)
        full_name_val = _norm_sig_field(full_name)
        att_type_val = _norm_sig_field(att_type)

        payload = {
            "date": date_val,
            "time": time_val,
            "full_name": full_name_val,
            "school": school,
            "nisn": nisn,
            "type": att_type_val,
            "office_name": office_name,
            "distance": distance,
            "activity": activity or "-",
            "filename": filename if filename != "-" else f"selfie_{date_val}_{time_val.replace(':', '')}.jpg",
            "image_base64": image_b64,
        }

        secret = (getattr(config, "GOOGLE_SHEET_WEBHOOK_SECRET", "") or "").strip()
        if secret:
            timestamp = str(int(time.time()))
            nonce = secrets.token_hex(16)
            payload["timestamp"] = timestamp
            payload["nonce"] = nonce
            payload["signature"] = build_webhook_signature(
                secret=secret,
                timestamp=timestamp,
                nonce=nonce,
                date_str=date_val,
                time_str=time_val,
                full_name=full_name_val,
                att_type=att_type_val,
            )
        else:
            logger.warning("⚠️ GOOGLE_SHEET_WEBHOOK_SECRET belum diset; payload webhook tidak ditandatangani.")

        # Google Apps Script melakukan redirect HTTP 302 ke script.googleusercontent.com;
        # event hook memastikan target redirect tetap HTTPS.
        async with httpx.AsyncClient(
            timeout=35.0,
            follow_redirects=True,
            max_redirects=5,
            event_hooks={"request": [_enforce_https_hook]},
        ) as client:
            response = await client.post(webhook_url, json=payload)
            if response.status_code == 200:
                logger.info("✅ Berhasil menyinkronkan absensi %s ke Google Sheets & Drive.", full_name)
            else:
                preview = (response.text or "")[:200]
                logger.warning("⚠️ Google Sheets Webhook merespons status %d (%s)", response.status_code, preview)
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
    photo_path: str,
    activity: str = "-",
):
    """Menjalankan sinkronisasi secara non-blocking di background dengan pelacakan task."""
    if config.GOOGLE_SHEET_WEBHOOK_URL:
        task = asyncio.create_task(
            sync_attendance_to_sheets(
                full_name=full_name,
                school=school,
                nisn=nisn,
                att_type=att_type,
                office_name=office_name,
                date_str=date_str,
                time_str=time_str,
                distance=distance,
                photo_path=photo_path,
                activity=activity,
            )
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)


async def wait_pending_syncs(timeout: float = 5.0) -> None:
    """Menunggu atau membatalkan sisa task sinkronisasi saat bot shutdown."""
    if not _background_tasks:
        return
    pending = list(_background_tasks)
    done, still_pending = await asyncio.wait(pending, timeout=timeout)
    for task in still_pending:
        task.cancel()
    if still_pending:
        await asyncio.gather(*still_pending, return_exceptions=True)
