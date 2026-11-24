import asyncio
import html
import logging
import math
import re
import sys
import weakref
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatType
from telegram.error import RetryAfter
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config
import database
import recap
import sheets_sync
import utils

# Setup logging (SEC-09: redam log INFO dari httpx/httpcore agar BOT_TOKEN tidak bocor)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# State untuk ConversationHandler
REG_NAME, REG_SCHOOL, REG_NISN = range(3)
CHECKIN_LOC, CHECKIN_PHOTO = range(3, 5)
CHECKOUT_LOC, CHECKOUT_PHOTO, CHECKOUT_ACTIVITY = range(5, 8)
LEAVE_TYPE, LEAVE_REASON, LEAVE_PHOTO = range(8, 11)
ACTIVITY_TEXT = 11

# Batasan validasi input
MAX_NAME_LEN = 100
MAX_SCHOOL_LEN = 100
MAX_TEXT_LEN = 1000
NISN_REGEX = re.compile(r"^\d{5,20}$")
CONVERSATION_TIMEOUT_SECONDS = 600

# Cooldown in-memory jika seluruh pengiriman rekap harian gagal (mencegah spam retry tiap 30 detik)
_RECAP_FAILURE_COOLDOWN_SECONDS = 300
_recap_last_failed_attempt: Dict[str, datetime] = {}
_scheduler_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


def _get_scheduler_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _scheduler_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _scheduler_locks[loop] = lock
    return lock


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else "-"))


def _is_private_message(update: Update) -> bool:
    return bool(
        update
        and update.effective_user is not None
        and update.effective_chat is not None
        and update.message is not None
        and update.effective_chat.type == ChatType.PRIVATE
    )


def _is_valid_coordinate(lat: float, lon: float) -> bool:
    return (
        math.isfinite(lat)
        and math.isfinite(lon)
        and (-90.0 <= lat <= 90.0)
        and (-180.0 <= lon <= 180.0)
    )


async def _send_message_throttled(bot, **kwargs) -> None:
    try:
        await bot.send_message(**kwargs)
    except RetryAfter as exc:
        delay = float(getattr(exc, "retry_after", 1.0)) + 0.5
        await asyncio.sleep(min(delay, 10.0))
        await bot.send_message(**kwargs)


def get_main_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """Membuat keyboard menu utama."""
    now = utils.get_now()
    is_weekend = now.weekday() in (5, 6)  # Sabtu (5) atau Minggu (6)

    btn_in = KeyboardButton("🟢 Absen Masuk")
    btn_out = KeyboardButton("🔴 Absen Pulang")

    row1 = [btn_in, btn_out]
    row2 = [KeyboardButton("📝 Laporan Kegiatan"), KeyboardButton("📋 Riwayat Absensi")]
    row3 = [KeyboardButton("👤 Profil Saya"), KeyboardButton("📄 Ajukan Izin / Sakit")]
    if is_weekend:
        row3.append(KeyboardButton("🏖️ Libur Hari Ini"))

    keyboard = [row1, row2, row3]
    if is_admin:
        keyboard.append([KeyboardButton("📊 Rekap Absensi (Admin)")])

    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_cancel_keyboard() -> ReplyKeyboardMarkup:
    """Keyboard pembatalan."""
    return ReplyKeyboardMarkup([[KeyboardButton("❌ Batal")]], resize_keyboard=True)


def get_location_keyboard() -> ReplyKeyboardMarkup:
    """Keyboard untuk membagikan lokasi GPS."""
    keyboard = [
        [KeyboardButton("📍 Kirim Lokasi Saat Ini", request_location=True)],
        [KeyboardButton("❌ Batal")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# ==========================================================
# 1. HANDLER PENDAFTARAN & START
# ==========================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private_message(update):
        return ConversationHandler.END

    user = update.effective_user
    db_user = database.get_user(user.id)
    is_admin = user.id in config.ADMIN_IDS

    if db_user:
        user_status = db_user.get("status", "pending")
        if user_status == "pending":
            pending_text = (
                f"👋 Halo, <b>{_esc(db_user['full_name'])}</b>!\n\n"
                f"⏳ Pendaftaran Anda saat ini <b>Menunggu Persetujuan (Approval) Admin</b>.\n\n"
                f"• Asal Sekolah: <b>{_esc(db_user.get('school', '-'))}</b>\n"
                f"• NISN: <code>{_esc(db_user.get('nisn', '-'))}</code>\n\n"
                f"📌 Anda akan menerima notifikasi otomatis di sini segera setelah disetujui oleh Admin."
            )
            await update.message.reply_text(pending_text, parse_mode="HTML")
            return ConversationHandler.END
        elif user_status == "rejected":
            rejected_text = (
                f"👋 Halo, <b>{_esc(db_user['full_name'])}</b>.\n\n"
                f"⛔ Pendaftaran Anda telah <b>Ditolak</b> oleh Admin.\n"
                f"Silakan hubungi pihak pembimbing/sekolah jika ada kesalahan data."
            )
            await update.message.reply_text(rejected_text, parse_mode="HTML")
            return ConversationHandler.END

        welcome_text = (
            f"👋 Halo, <b>{_esc(db_user['full_name'])}</b>!\n\n"
            f"Selamat datang kembali di <b>Sistem Absensi Telegram</b>.\n"
            f"Silakan pilih menu di bawah ini untuk memulai absensi."
        )
        await update.message.reply_text(
            welcome_text,
            reply_markup=get_main_keyboard(is_admin),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    intro_text = (
        f"👋 Halo, <b>{_esc(user.first_name)}</b>!\n\n"
        f"Selamat datang di <b>Sistem Absensi Prakerin/Magang</b>.\n"
        f"Anda belum terdaftar dalam sistem. Mari lengkapi data pendaftaran Anda.\n\n"
        f"👉 Langkah 1: Silakan ketik <b>Nama Lengkap</b> Anda:"
    )
    await update.message.reply_text(intro_text, reply_markup=get_cancel_keyboard(), parse_mode="HTML")
    return REG_NAME


async def reg_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private_message(update):
        return ConversationHandler.END

    name = (update.message.text or "").strip()
    if not name or len(name) > MAX_NAME_LEN:
        await update.message.reply_text(
            f"Nama harus diisi (maksimal {MAX_NAME_LEN} karakter). Silakan ketik Nama Lengkap Anda:"
        )
        return REG_NAME

    context.user_data["reg_name"] = name
    await update.message.reply_text(
        f"Terima kasih, <b>{_esc(name)}</b>.\n\n👉 Langkah 2: Masukkan <b>Asal Sekolah / Institusi</b> Anda (contoh: <i>SMKN 1 Jombang</i>):",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML",
    )
    return REG_SCHOOL


async def reg_school_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private_message(update):
        return ConversationHandler.END

    school = (update.message.text or "").strip()
    if not school or len(school) > MAX_SCHOOL_LEN:
        await update.message.reply_text(
            f"Asal sekolah harus diisi (maksimal {MAX_SCHOOL_LEN} karakter). Silakan ketik Asal Sekolah Anda:"
        )
        return REG_SCHOOL

    context.user_data["reg_school"] = school
    await update.message.reply_text(
        f"Baik, sekolah Anda: <b>{_esc(school)}</b>.\n\n👉 Langkah 3: Sekarang masukkan <b>NISN</b> (5–20 digit angka):",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML",
    )
    return REG_NISN


async def reg_nisn_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private_message(update):
        return ConversationHandler.END

    nisn = (update.message.text or "").strip()
    if not NISN_REGEX.fullmatch(nisn):
        await update.message.reply_text(
            "NISN tidak valid. Harap masukkan 5–20 digit angka saja (contoh: 0051234567):",
            reply_markup=get_cancel_keyboard(),
        )
        return REG_NISN

    name = context.user_data.get("reg_name")
    school = context.user_data.get("reg_school")
    if not name or not school:
        context.user_data.clear()
        await update.message.reply_text(
            "⚠️ Sesi pendaftaran kadaluarsa. Silakan ketik /start untuk mengulang.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    user_id = update.effective_user.id
    is_admin = user_id in config.ADMIN_IDS

    role = "admin" if is_admin else "student"
    status = "approved" if is_admin else "pending"

    success = database.register_user(
        telegram_id=user_id,
        full_name=name,
        school=school,
        nisn=nisn,
        role=role,
        status=status,
    )

    if success:
        if is_admin:
            text = (
                f"🎉 <b>Pendaftaran Admin Berhasil!</b>\n\n"
                f"👤 <b>Nama:</b> {_esc(name)}\n"
                f"🏫 <b>Sekolah:</b> {_esc(school)}\n"
                f"🆔 <b>NISN:</b> <code>{_esc(nisn)}</code>\n"
                f"🔰 <b>Role:</b> Admin (Auto-Approved)\n\n"
                f"Anda dapat menggunakan seluruh menu absensi dan admin."
            )
            await update.message.reply_text(text, reply_markup=get_main_keyboard(True), parse_mode="HTML")
        else:
            text = (
                f"🎉 <b>Pendaftaran Berhasil Dikirim!</b>\n\n"
                f"📋 <b>Rincian Data Anda:</b>\n"
                f"• Nama: <b>{_esc(name)}</b>\n"
                f"• Asal Sekolah: <b>{_esc(school)}</b>\n"
                f"• NISN: <code>{_esc(nisn)}</code>\n\n"
                f"⏳ <i>Pendaftaran Anda telah diteruskan ke Admin untuk diverifikasi.</i>\n"
                f"Anda akan menerima pemberitahuan otomatis di sini setelah akun Anda disetujui (Approved)."
            )
            await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")

            admin_notify_text = (
                f"🔔 <b>Permintaan Pendaftaran Siswa Baru!</b>\n\n"
                f"👤 <b>Nama:</b> {_esc(name)}\n"
                f"🏫 <b>Asal Sekolah:</b> {_esc(school)}\n"
                f"🆔 <b>NISN:</b> <code>{_esc(nisn)}</code>\n"
                f"🆔 <b>ID Telegram:</b> <code>{user_id}</code>\n\n"
                f"Silakan periksa dan pilih tindakan di bawah:"
            )
            admin_markup = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Setujui", callback_data=f"approve_{user_id}"),
                    InlineKeyboardButton("❌ Tolak", callback_data=f"reject_{user_id}"),
                ]
            ])
            for admin_id in config.ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=admin_notify_text,
                        reply_markup=admin_markup,
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.warning("Gagal kirim notif ke admin %s: %s", admin_id, e)
    else:
        await update.message.reply_text(
            "❌ Terjadi kesalahan saat menyimpan pendaftaran (atau akun Anda sudah terdaftar). Silakan ketik /start.",
            reply_markup=ReplyKeyboardRemove(),
        )

    context.user_data.clear()
    return ConversationHandler.END


# ==========================================================
# 2. HANDLER ABSEN MASUK
# ==========================================================
async def checkin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private_message(update):
        return ConversationHandler.END

    user_id = update.effective_user.id
    db_user = database.get_user(user_id)
    is_admin = user_id in config.ADMIN_IDS

    if not db_user:
        await update.message.reply_text(
            "⚠️ Anda belum terdaftar. Silakan ketik /start untuk mendaftar.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    user_status = db_user.get("status", "pending")
    if user_status == "pending":
        await update.message.reply_text(
            "⏳ Pendaftaran Anda masih <b>Menunggu Persetujuan (Approval) Admin</b>.\n"
            "Anda belum dapat melakukan absensi sebelum disetujui.",
            parse_mode="HTML",
        )
        return ConversationHandler.END
    elif user_status == "rejected":
        await update.message.reply_text(
            "⛔ Pendaftaran Anda telah <b>Ditolak</b> oleh Admin.\n"
            "Silakan hubungi pihak pembimbing/sekolah.",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    is_late, entry_time_str, cur_time = utils.is_past_entry_cutoff()
    if is_late:
        late_text = (
            "❌ <b>Absen Masuk Ditolak!</b>\n\n"
            f"Batas waktu absensi masuk hari ini telah lewat.\n"
            f"• Batas Maksimal: <b>{_esc(entry_time_str)} WIB</b>\n"
            f"• Waktu Saat Ini: <b>{_esc(cur_time)} WIB</b>\n\n"
            "⚠️ Anda tidak diperkenankan absen karena telah melewati batas jam masuk.\n"
            "Silakan segera <b>hubungi pembimbing</b> untuk konfirmasi keterlambatan Anda."
        )
        await update.message.reply_text(late_text, reply_markup=get_main_keyboard(is_admin), parse_mode="HTML")
        return ConversationHandler.END

    today_str = utils.get_today_str()
    attendance = database.get_today_attendance(user_id, today_str)

    if attendance and attendance.get("check_in_time"):
        jam_masuk = attendance["check_in_time"]
        office_name = attendance.get("check_in_office") or "Kantor"
        await update.message.reply_text(
            f"ℹ️ Anda sudah melakukan <b>Absen Masuk</b> hari ini di <b>{_esc(office_name)}</b> pada pukul <b>{_esc(jam_masuk)} WIB</b>.\n"
            f"Jangan lupa untuk <b>Absen Pulang</b> nanti setelah selesai bekerja ya!",
            reply_markup=get_main_keyboard(is_admin),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["checkin_date"] = today_str

    text = (
        "🟢 <b>Proses Absen Masuk</b>\n\n"
        "Langkah 1: Silakan bagikan lokasi GPS Anda saat ini dengan menekan tombol <b>'📍 Kirim Lokasi Saat Ini'</b> di bawah."
    )
    await update.message.reply_text(text, reply_markup=get_location_keyboard(), parse_mode="HTML")
    return CHECKIN_LOC


async def checkin_location_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private_message(update):
        return ConversationHandler.END

    user_id = update.effective_user.id
    is_admin = user_id in config.ADMIN_IDS

    db_user = database.get_user(user_id)
    if not db_user or db_user.get("status") != "approved":
        context.user_data.clear()
        await update.message.reply_text("⛔ Akun Anda tidak aktif / belum disetujui.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    is_late, entry_time_str, _ = utils.is_past_entry_cutoff()
    if is_late:
        await update.message.reply_text(
            f"❌ <b>Absen Masuk Ditolak!</b>\n\nBatas maksimal jam masuk ({_esc(entry_time_str)} WIB) telah lewat.\nSilakan segera <b>hubungi pembimbing</b>.",
            reply_markup=get_main_keyboard(is_admin),
            parse_mode="HTML",
        )
        context.user_data.clear()
        return ConversationHandler.END

    # SEC-07: Tolak lokasi terusan (forward) atau dari bot lain
    if update.message.forward_origin is not None or update.message.via_bot is not None:
        await update.message.reply_text(
            "⛔ Lokasi terusan (forwarded) atau dari bot lain tidak diterima. Silakan kirim lokasi GPS langsung saat ini.",
            reply_markup=get_location_keyboard(),
        )
        return CHECKIN_LOC

    location = update.message.location
    if not location:
        await update.message.reply_text(
            "Mohon tekan tombol '📍 Kirim Lokasi Saat Ini' untuk membagikan koordinat GPS Anda.",
            reply_markup=get_location_keyboard(),
        )
        return CHECKIN_LOC

    lat = location.latitude
    lon = location.longitude
    if not _is_valid_coordinate(lat, lon):
        await update.message.reply_text(
            "⚠️ Koordinat GPS tidak valid. Silakan kirim ulang lokasi Anda.",
            reply_markup=get_location_keyboard(),
        )
        return CHECKIN_LOC

    check_res = utils.check_proximity_all_offices(lat, lon, config.OFFICES)

    if not check_res["is_valid"]:
        lines = [
            "❌ <b>Absen Masuk Ditolak!</b>\n",
            "Anda berada di luar radius kantor yang diizinkan:\n",
        ]
        for d in check_res["details"]:
            radius_text = f"{d['radius']:,.0f} meter" if d["radius"] > 0 else "Tidak valid"
            lines.append(f"• <b>{_esc(d['name'])}</b>: Jarak {d['distance']:,.0f} m (Maks: {radius_text})")
        lines.append("\nSilakan pastikan GPS akurat dan Anda sudah berada di salah satu lokasi kantor.")

        await update.message.reply_text("\n".join(lines), reply_markup=get_main_keyboard(is_admin), parse_mode="HTML")
        context.user_data.clear()
        return ConversationHandler.END

    matched_office = check_res["matched_office"]
    office_name = matched_office["name"]
    distance = check_res["distance"]

    context.user_data["checkin_lat"] = lat
    context.user_data["checkin_lon"] = lon
    context.user_data["checkin_distance"] = distance
    context.user_data["checkin_office"] = office_name
    context.user_data.setdefault("checkin_date", utils.get_today_str())

    text = (
        f"✅ <b>Lokasi Terverifikasi di {_esc(office_name)}!</b> 🏢\n"
        f"📍 Jarak dari titik kantor: <b>{distance:,.0f} meter</b>\n\n"
        f"Langkah 2: Sekarang silakan <b>kirimkan foto selfie Anda</b> sebagai bukti kehadiran:"
    )
    await update.message.reply_text(text, reply_markup=get_cancel_keyboard(), parse_mode="HTML")
    return CHECKIN_PHOTO


async def checkin_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private_message(update):
        return ConversationHandler.END

    photos = update.message.photo
    if not photos:
        await update.message.reply_text(
            "Silakan kirimkan foto selfie langsung dari kamera Anda.",
            reply_markup=get_cancel_keyboard(),
        )
        return CHECKIN_PHOTO

    user_id = update.effective_user.id
    is_admin = user_id in config.ADMIN_IDS
    today_str = utils.get_today_str()
    time_str = utils.get_current_time_str()

    # SEC-06: Re-validasi status, state sesi, tanggal, cutoff, & duplikasi di langkah akhir
    db_user = database.get_user(user_id)
    if not db_user or db_user.get("status") != "approved":
        context.user_data.clear()
        await update.message.reply_text("⛔ Akun Anda tidak aktif / belum disetujui.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    required_keys = ("checkin_lat", "checkin_lon", "checkin_distance", "checkin_office", "checkin_date")
    if any(k not in context.user_data for k in required_keys):
        context.user_data.clear()
        await update.message.reply_text(
            "⚠️ Sesi absen masuk tidak lengkap atau telah kadaluarsa. Silakan mulai ulang.",
            reply_markup=get_main_keyboard(is_admin),
        )
        return ConversationHandler.END

    if context.user_data["checkin_date"] != today_str:
        context.user_data.clear()
        await update.message.reply_text(
            "⚠️ Sesi absen masuk melewati pergantian hari. Silakan mulai absen masuk ulang.",
            reply_markup=get_main_keyboard(is_admin),
        )
        return ConversationHandler.END

    is_late, entry_time_str, _ = utils.is_past_entry_cutoff()
    if is_late:
        context.user_data.clear()
        await update.message.reply_text(
            f"❌ <b>Absen Masuk Ditolak!</b>\n\nBatas maksimal jam masuk ({_esc(entry_time_str)} WIB) telah lewat saat foto dikirim.\nSilakan segera <b>hubungi pembimbing</b>.",
            reply_markup=get_main_keyboard(is_admin),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    existing_att = database.get_today_attendance(user_id, today_str)
    if existing_att and existing_att.get("check_in_time"):
        context.user_data.clear()
        await update.message.reply_text(
            "ℹ️ Anda sudah tercatat melakukan Absen Masuk hari ini.",
            reply_markup=get_main_keyboard(is_admin),
        )
        return ConversationHandler.END

    photo = photos[-1]
    photo_file = await photo.get_file()

    filename = f"checkin_{user_id}_{today_str}_{time_str.replace(':', '')}.jpg"
    filepath = config.PHOTOS_DIR / filename
    await photo_file.download_to_drive(custom_path=filepath)

    lat = float(context.user_data["checkin_lat"])
    lon = float(context.user_data["checkin_lon"])
    distance = float(context.user_data["checkin_distance"])
    office_name = str(context.user_data["checkin_office"])

    success = database.record_check_in(
        telegram_id=user_id,
        date_str=today_str,
        time_str=time_str,
        lat=lat,
        lon=lon,
        distance=distance,
        photo_path=str(filepath),
        office_name=office_name,
        status="Hadir",
    )

    if success:
        sheets_sync.trigger_sheet_sync(
            full_name=db_user.get("full_name", "Siswa"),
            school=db_user.get("school", "-"),
            nisn=db_user.get("nisn", "-"),
            att_type="Masuk",
            office_name=office_name,
            date_str=today_str,
            time_str=time_str,
            distance=distance,
            photo_path=str(filepath),
        )

        text = (
            f"🎉 <b>Absen Masuk Berhasil!</b>\n\n"
            f"🏢 Lokasi: <b>{_esc(office_name)}</b>\n"
            f"📅 Tanggal: <b>{_esc(today_str)}</b>\n"
            f"🕒 Jam Masuk: <b>{_esc(time_str)} WIB</b>\n"
            f"📍 Jarak: <b>{distance:,.0f} meter</b>\n\n"
            f"Selamat bertugas dan semoga hari Anda produktif!"
        )
    else:
        filepath.unlink(missing_ok=True)
        text = "❌ Terjadi kegagalan sistem saat mencatat absensi. Silakan coba lagi."

    await update.message.reply_text(text, reply_markup=get_main_keyboard(is_admin), parse_mode="HTML")
    context.user_data.clear()
    return ConversationHandler.END


# ==========================================================
# 3. HANDLER ABSEN PULANG
# ==========================================================
async def checkout_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private_message(update):
        return ConversationHandler.END

    user_id = update.effective_user.id
    db_user = database.get_user(user_id)
    is_admin = user_id in config.ADMIN_IDS

    if not db_user:
        await update.message.reply_text("⚠️ Anda belum terdaftar. Silakan ketik /start untuk mendaftar.")
        return ConversationHandler.END

    user_status = db_user.get("status", "pending")
    if user_status == "pending":
        await update.message.reply_text(
            "⏳ Pendaftaran Anda masih <b>Menunggu Persetujuan (Approval) Admin</b>.",
            parse_mode="HTML",
        )
        return ConversationHandler.END
    elif user_status == "rejected":
        await update.message.reply_text(
            "⛔ Pendaftaran Anda telah <b>Ditolak</b> oleh Admin.",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    today_str = utils.get_today_str()
    attendance = database.get_today_attendance(user_id, today_str)

    if not attendance or not attendance.get("check_in_time") or attendance.get("check_in_time") == "-":
        await update.message.reply_text(
            "⚠️ Anda belum melakukan <b>Absen Masuk</b> hari ini.\n\n"
            "ℹ️ <b>Ketentuan Cek Log Pulang:</b>\n"
            "• Absen pulang hanya dapat dilakukan pada hari yang sama setelah absen masuk.\n"
            f"• Absen pulang dibuka mulai pukul <b>{config.CHECKOUT_OPEN_HOUR:02d}:{config.CHECKOUT_OPEN_MINUTE:02d} WIB</b> hingga maksimal <b>23:59 WIB</b>.",
            reply_markup=get_main_keyboard(is_admin),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if attendance.get("check_out_time"):
        jam_pulang = attendance["check_out_time"]
        office_name = attendance.get("check_out_office") or "Kantor"
        await update.message.reply_text(
            f"ℹ️ Anda sudah melakukan <b>Absen Pulang</b> hari ini di <b>{_esc(office_name)}</b> pada pukul <b>{_esc(jam_pulang)} WIB</b>.\n"
            f"Sampai jumpa besok!",
            reply_markup=get_main_keyboard(is_admin),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    is_open, open_time_str, cur_time_str = utils.is_checkout_open()
    if not is_open:
        if not is_admin:
            await update.message.reply_text(
                f"⏳ <b>Absen Pulang Belum Dibuka!</b>\n\n"
                f"Absen Pulang baru dibuka mulai pukul <b>{_esc(open_time_str)} WIB</b> "
                f"hingga maksimal pukul <b>23:59 WIB</b>.\n\n"
                f"🕒 Waktu saat ini: <code>{_esc(cur_time_str)} WIB</code>\n"
                f"Silakan kembali lagi nanti setelah jam kerja selesai ya!",
                reply_markup=get_main_keyboard(is_admin),
                parse_mode="HTML",
            )
            return ConversationHandler.END
        else:
            await update.message.reply_text(
                f"ℹ️ <b>[Mode Admin]</b> Saat ini pukul <code>{_esc(cur_time_str)} WIB</code> (sebelum jam buka {_esc(open_time_str)} WIB).\n"
                f"Akses admin mengizinkan untuk melanjutkan pengujian absen pulang.",
                parse_mode="HTML",
            )

    context.user_data.clear()
    context.user_data["checkout_date"] = today_str

    text = (
        "🔴 <b>Proses Absen Pulang</b>\n\n"
        "Langkah 1: Silakan bagikan lokasi GPS Anda dengan menekan tombol <b>'📍 Kirim Lokasi Saat Ini'</b> di bawah."
    )
    await update.message.reply_text(text, reply_markup=get_location_keyboard(), parse_mode="HTML")
    return CHECKOUT_LOC


async def checkout_location_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private_message(update):
        return ConversationHandler.END

    user_id = update.effective_user.id
    is_admin = user_id in config.ADMIN_IDS

    db_user = database.get_user(user_id)
    if not db_user or db_user.get("status") != "approved":
        context.user_data.clear()
        await update.message.reply_text("⛔ Akun Anda tidak aktif / belum disetujui.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    if update.message.forward_origin is not None or update.message.via_bot is not None:
        await update.message.reply_text(
            "⛔ Lokasi terusan (forwarded) atau dari bot lain tidak diterima. Silakan kirim lokasi GPS langsung saat ini.",
            reply_markup=get_location_keyboard(),
        )
        return CHECKOUT_LOC

    location = update.message.location
    if not location:
        await update.message.reply_text(
            "Mohon tekan tombol '📍 Kirim Lokasi Saat Ini' untuk membagikan koordinat GPS Anda.",
            reply_markup=get_location_keyboard(),
        )
        return CHECKOUT_LOC

    lat = location.latitude
    lon = location.longitude
    if not _is_valid_coordinate(lat, lon):
        await update.message.reply_text(
            "⚠️ Koordinat GPS tidak valid. Silakan kirim ulang lokasi Anda.",
            reply_markup=get_location_keyboard(),
        )
        return CHECKOUT_LOC

    check_res = utils.check_proximity_all_offices(lat, lon, config.OFFICES)

    if not check_res["is_valid"]:
        lines = [
            "❌ <b>Absen Pulang Ditolak!</b>\n",
            "Anda berada di luar radius kantor yang diizinkan:\n",
        ]
        for d in check_res["details"]:
            radius_text = f"{d['radius']:,.0f} meter" if d["radius"] > 0 else "Tidak valid"
            lines.append(f"• <b>{_esc(d['name'])}</b>: Jarak {d['distance']:,.0f} m (Maks: {radius_text})")
        lines.append("\nSilakan pastikan Anda berada di salah satu lokasi kantor.")

        await update.message.reply_text("\n".join(lines), reply_markup=get_main_keyboard(is_admin), parse_mode="HTML")
        context.user_data.clear()
        return ConversationHandler.END

    matched_office = check_res["matched_office"]
    office_name = matched_office["name"]
    distance = check_res["distance"]

    context.user_data["checkout_lat"] = lat
    context.user_data["checkout_lon"] = lon
    context.user_data["checkout_distance"] = distance
    context.user_data["checkout_office"] = office_name
    context.user_data.setdefault("checkout_date", utils.get_today_str())

    text = (
        f"✅ <b>Lokasi Terverifikasi di {_esc(office_name)}!</b> 🏢\n"
        f"📍 Jarak dari titik kantor: <b>{distance:,.0f} meter</b>\n\n"
        f"Langkah 2: Sekarang silakan <b>kirimkan foto selfie Anda</b> sebagai bukti absensi pulang:"
    )
    await update.message.reply_text(text, reply_markup=get_cancel_keyboard(), parse_mode="HTML")
    return CHECKOUT_PHOTO


async def checkout_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private_message(update):
        return ConversationHandler.END

    photos = update.message.photo
    if not photos:
        await update.message.reply_text("Silakan kirimkan foto selfie Anda.", reply_markup=get_cancel_keyboard())
        return CHECKOUT_PHOTO

    user_id = update.effective_user.id
    is_admin = user_id in config.ADMIN_IDS
    today_str = utils.get_today_str()
    time_str = utils.get_current_time_str()

    if context.user_data.get("checkout_date") != today_str or "checkout_office" not in context.user_data:
        context.user_data.clear()
        await update.message.reply_text(
            "⚠️ Sesi absen pulang kadaluarsa atau tidak valid. Silakan mulai ulang.",
            reply_markup=get_main_keyboard(is_admin),
        )
        return ConversationHandler.END

    photo = photos[-1]
    photo_file = await photo.get_file()

    filename = f"checkout_{user_id}_{today_str}_{time_str.replace(':', '')}.jpg"
    filepath = config.PHOTOS_DIR / filename
    await photo_file.download_to_drive(custom_path=filepath)

    context.user_data["checkout_photo_path"] = str(filepath)

    text = (
        "📸 <b>Foto Selfie Berhasil Diterima!</b>\n\n"
        "📝 <b>Langkah Terakhir: Laporan Kegiatan Harian</b>\n\n"
        "Silakan tuliskan ringkasan kegiatan / pekerjaan magang yang Anda lakukan seharian ini:\n"
        "<i>(Contoh: 1. Merapikan kabel jaringan, 2. Backup database harian, 3. Input berkas inventaris)</i>"
    )
    await update.message.reply_text(text, reply_markup=get_cancel_keyboard(), parse_mode="HTML")
    return CHECKOUT_ACTIVITY


async def checkout_activity_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private_message(update):
        return ConversationHandler.END

    activity_text = (update.message.text or "").strip()
    if not activity_text or len(activity_text) > MAX_TEXT_LEN:
        await update.message.reply_text(
            f"Mohon ketikkan ringkasan kegiatan Anda hari ini (1–{MAX_TEXT_LEN} karakter):",
            reply_markup=get_cancel_keyboard(),
        )
        return CHECKOUT_ACTIVITY

    user_id = update.effective_user.id
    is_admin = user_id in config.ADMIN_IDS
    today_str = utils.get_today_str()
    time_str = utils.get_current_time_str()

    saved_photo = context.user_data.get("checkout_photo_path", "")

    db_user = database.get_user(user_id)
    if not db_user or db_user.get("status") != "approved":
        if saved_photo:
            Path(saved_photo).unlink(missing_ok=True)
        context.user_data.clear()
        await update.message.reply_text("⛔ Akun Anda tidak aktif / belum disetujui.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    required_keys = (
        "checkout_lat",
        "checkout_lon",
        "checkout_distance",
        "checkout_office",
        "checkout_photo_path",
        "checkout_date",
    )
    if any(k not in context.user_data for k in required_keys) or context.user_data["checkout_date"] != today_str:
        if saved_photo:
            Path(saved_photo).unlink(missing_ok=True)
        context.user_data.clear()
        await update.message.reply_text(
            "⚠️ Sesi absen pulang kadaluarsa atau melewati pergantian hari. Silakan mulai ulang.",
            reply_markup=get_main_keyboard(is_admin),
        )
        return ConversationHandler.END

    is_open, open_time_str, _ = utils.is_checkout_open()
    if not is_open and not is_admin:
        if saved_photo:
            Path(saved_photo).unlink(missing_ok=True)
        context.user_data.clear()
        await update.message.reply_text(
            f"⏳ Absen pulang baru dibuka mulai pukul <b>{_esc(open_time_str)} WIB</b>.",
            reply_markup=get_main_keyboard(is_admin),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    lat = float(context.user_data["checkout_lat"])
    lon = float(context.user_data["checkout_lon"])
    distance = float(context.user_data["checkout_distance"])
    office_name = str(context.user_data["checkout_office"])
    filepath = str(context.user_data["checkout_photo_path"])

    success = database.record_check_out(
        telegram_id=user_id,
        date_str=today_str,
        time_str=time_str,
        lat=lat,
        lon=lon,
        distance=distance,
        photo_path=filepath,
        office_name=office_name,
        activity=activity_text,
    )

    if success:
        sheets_sync.trigger_sheet_sync(
            full_name=db_user.get("full_name", "Siswa"),
            school=db_user.get("school", "-"),
            nisn=db_user.get("nisn", "-"),
            att_type="Pulang",
            office_name=office_name,
            date_str=today_str,
            time_str=time_str,
            distance=distance,
            photo_path=filepath,
            activity=activity_text,
        )

        text = (
            f"🎉 <b>Absen Pulang &amp; Laporan Kegiatan Berhasil!</b>\n\n"
            f"🏢 Lokasi: <b>{_esc(office_name)}</b>\n"
            f"📅 Tanggal: <b>{_esc(today_str)}</b>\n"
            f"🕒 Jam Pulang: <b>{_esc(time_str)} WIB</b>\n"
            f"📍 Jarak Kantor: <b>{distance:,.0f} meter</b>\n"
            f"📝 <b>Laporan Kegiatan:</b>\n<i>{_esc(activity_text)}</i>\n\n"
            f"Terima kasih atas kerja keras Anda hari ini. Selamat beristirahat!"
        )
    else:
        if filepath:
            Path(filepath).unlink(missing_ok=True)
        text = "❌ Terjadi kegagalan sistem saat mencatat absensi pulang (pastikan Anda sudah absen masuk hari ini dan belum pulang)."

    await update.message.reply_text(text, reply_markup=get_main_keyboard(is_admin), parse_mode="HTML")
    context.user_data.clear()
    return ConversationHandler.END


# ==========================================================
# 4. HANDLER LAPORAN KEGIATAN HARIAN (MANDIRI)
# ==========================================================
async def activity_report_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Memulai proses pengisian atau pembaruan laporan kegiatan harian."""
    if not _is_private_message(update):
        return ConversationHandler.END

    user_id = update.effective_user.id
    db_user = database.get_user(user_id)
    is_admin = user_id in config.ADMIN_IDS

    if not db_user:
        await update.message.reply_text("⚠️ Anda belum terdaftar. Silakan ketik /start untuk mendaftar.")
        return ConversationHandler.END

    if db_user.get("status") != "approved":
        await update.message.reply_text("⏳ Akun Anda belum disetujui Admin.")
        return ConversationHandler.END

    today_str = utils.get_today_str()
    att = database.get_today_attendance(user_id, today_str)

    if not att or not att.get("check_in_time") or att.get("check_in_time") == "-":
        await update.message.reply_text(
            "⚠️ Anda belum melakukan <b>Absen Masuk</b> hari ini.\n"
            "Laporan kegiatan hanya dapat diisi jika Anda telah hadir bekerja.",
            reply_markup=get_main_keyboard(is_admin),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    current_act = att.get("activity")
    if current_act:
        prompt = (
            f"📝 <b>Laporan Kegiatan Hari Ini ({_esc(today_str)}):</b>\n\n"
            f"<i>{_esc(current_act)}</i>\n\n"
            f"✏️ <b>Ingin memperbarui isi laporan kegiatan?</b>\n"
            f"Silakan ketikkan teks laporan baru di bawah, atau tekan <b>❌ Batal</b> jika tidak ingin mengubah."
        )
    else:
        prompt = (
            f"📝 <b>Input Laporan Kegiatan Hari Ini ({_esc(today_str)})</b>\n\n"
            f"Silakan ketikkan rangkuman kegiatan / pekerjaan yang Anda kerjakan hari ini:\n"
            f"<i>(Contoh: 1. Merapikan kabel jaringan, 2. Backup database harian, 3. Input berkas inventaris)</i>"
        )

    await update.message.reply_text(prompt, reply_markup=get_cancel_keyboard(), parse_mode="HTML")
    return ACTIVITY_TEXT


async def activity_report_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private_message(update):
        return ConversationHandler.END

    activity_text = (update.message.text or "").strip()
    if not activity_text or len(activity_text) > MAX_TEXT_LEN:
        await update.message.reply_text(
            f"Mohon ketikkan laporan kegiatan Anda (1–{MAX_TEXT_LEN} karakter):",
            reply_markup=get_cancel_keyboard(),
        )
        return ACTIVITY_TEXT

    user_id = update.effective_user.id
    is_admin = user_id in config.ADMIN_IDS
    today_str = utils.get_today_str()
    time_str = utils.get_current_time_str()

    db_user = database.get_user(user_id)
    if not db_user or db_user.get("status") != "approved":
        await update.message.reply_text("⛔ Akun Anda tidak aktif / belum disetujui.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    success = database.update_activity_report(user_id, today_str, activity_text)
    if success:
        att = database.get_today_attendance(user_id, today_str)
        sheets_sync.trigger_sheet_sync(
            full_name=db_user.get("full_name", "Siswa"),
            school=db_user.get("school", "-"),
            nisn=db_user.get("nisn", "-"),
            att_type="Laporan",
            office_name=att.get("check_in_office", "-") if att else "-",
            date_str=today_str,
            time_str=time_str,
            distance=0,
            photo_path="",
            activity=activity_text,
        )

        await update.message.reply_text(
            f"✅ <b>Laporan Kegiatan Berhasil Disimpan!</b>\n\n"
            f"📅 Tanggal: <b>{_esc(today_str)}</b>\n"
            f"📝 <b>Kegiatan:</b>\n<i>{_esc(activity_text)}</i>\n\n"
            f"Laporan kegiatan telah tercatat di sistem &amp; Google Sheets.",
            reply_markup=get_main_keyboard(is_admin),
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            "❌ Gagal menyimpan laporan kegiatan. Pastikan Anda sudah absen masuk hari ini.",
            reply_markup=get_main_keyboard(is_admin),
        )

    return ConversationHandler.END


# ==========================================================
# 5. HANDLER LIBUR AKHIR PEKAN (SABTU & MINGGU)
# ==========================================================
async def day_off_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Konfirmasi libur mandiri oleh siswa pada hari Sabtu atau Minggu."""
    if not _is_private_message(update):
        return

    user_id = update.effective_user.id
    db_user = database.get_user(user_id)
    is_admin = user_id in config.ADMIN_IDS

    if not db_user:
        await update.message.reply_text("⚠️ Anda belum terdaftar. Silakan ketik /start.")
        return

    if db_user.get("status") != "approved":
        await update.message.reply_text("⏳ Akun Anda masih menunggu persetujuan Admin.")
        return

    now = utils.get_now()
    if now.weekday() not in (5, 6):
        await update.message.reply_text(
            "⚠️ Tombol <b>Libur Hari Ini</b> hanya dapat digunakan pada hari Sabtu dan Minggu.\n"
            "Untuk hari kerja biasa (Senin - Jumat), silakan gunakan menu <b>'📄 Ajukan Izin / Sakit'</b> jika Anda berhalangan hadir.",
            parse_mode="HTML",
        )
        return

    today_str = utils.get_today_str()
    att = database.get_today_attendance(user_id, today_str)

    if att:
        if att.get("status") == "Libur":
            await update.message.reply_text(
                "ℹ️ Status kehadiran Anda hari ini sudah tercatat sebagai <b>Libur</b>.",
                parse_mode="HTML",
            )
            return
        elif att.get("check_in_time") and att.get("check_in_time") != "-":
            await update.message.reply_text(
                f"⚠️ Anda sudah melakukan absen masuk hari ini pada pukul {_esc(att['check_in_time'])} WIB."
            )
            return

    if not database.record_day_off(user_id, today_str):
        await update.message.reply_text(
            "⚠️ Tidak dapat mencatat status Libur (kemungkinan sudah terdapat data absensi Hadir hari ini).",
            reply_markup=get_main_keyboard(is_admin),
        )
        return

    sheets_sync.trigger_sheet_sync(
        full_name=db_user.get("full_name", "Siswa"),
        school=db_user.get("school", "-"),
        nisn=db_user.get("nisn", "-"),
        att_type="Libur",
        office_name="-",
        date_str=today_str,
        time_str="-",
        distance=0,
        photo_path="",
    )

    day_name = "Sabtu" if now.weekday() == 5 else "Minggu"
    confirm_text = (
        f"🏖️ <b>Konfirmasi Libur Berhasil!</b>\n\n"
        f"📅 Tanggal: <b>{_esc(today_str)} ({day_name})</b>\n"
        f"📌 Status: <b>Libur Akhir Pekan</b>\n\n"
        f"Status kehadiran Anda telah dicatat. Selamat beristirahat dan menikmati akhir pekan!"
    )
    await update.message.reply_text(confirm_text, reply_markup=get_main_keyboard(is_admin), parse_mode="HTML")


# ==========================================================
# 6. HANDLER PENGAJUAN IZIN / SAKIT
# ==========================================================
async def leave_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Memulai alur pengajuan izin / sakit."""
    if not _is_private_message(update):
        return ConversationHandler.END

    user_id = update.effective_user.id
    db_user = database.get_user(user_id)
    is_admin = user_id in config.ADMIN_IDS

    if not db_user:
        await update.message.reply_text("⚠️ Anda belum terdaftar. Silakan ketik /start.")
        return ConversationHandler.END

    if db_user.get("status") != "approved":
        await update.message.reply_text("⏳ Akun Anda masih menunggu persetujuan Admin.")
        return ConversationHandler.END

    today_str = utils.get_today_str()
    att = database.get_today_attendance(user_id, today_str)
    if att and att.get("status") in ("Sakit", "Izin", "Libur", "Hadir"):
        await update.message.reply_text(
            f"ℹ️ Anda sudah memiliki catatan status <b>{_esc(att['status'])}</b> untuk hari ini ({_esc(today_str)}).",
            reply_markup=get_main_keyboard(is_admin),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    leave_type_keyboard = ReplyKeyboardMarkup([
        [KeyboardButton("🤒 Sakit"), KeyboardButton("📝 Izin / Dispensasi")],
        [KeyboardButton("❌ Batal")],
    ], resize_keyboard=True)

    text = (
        "📄 <b>Pengajuan Izin / Sakit</b>\n\n"
        "Silakan pilih kategori ketidakhadiran Anda di bawah:"
    )
    await update.message.reply_text(text, reply_markup=leave_type_keyboard, parse_mode="HTML")
    return LEAVE_TYPE


async def leave_type_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private_message(update):
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    if "sakit" in text.lower():
        leave_type = "Sakit"
    elif "izin" in text.lower() or "dispensasi" in text.lower():
        leave_type = "Izin"
    else:
        await update.message.reply_text("Silakan pilih tombol '🤒 Sakit' atau '📝 Izin / Dispensasi':")
        return LEAVE_TYPE

    context.user_data["leave_type"] = leave_type
    prompt = (
        f"Kategori: <b>{_esc(leave_type)}</b>\n\n"
        f"👉 Silakan ketik <b>alasan / keterangan</b> Anda (contoh: <i>Demam tinggi periksa ke dokter</i> atau <i>Ujian sekolah</i>):"
    )
    await update.message.reply_text(prompt, reply_markup=get_cancel_keyboard(), parse_mode="HTML")
    return LEAVE_REASON


async def leave_reason_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private_message(update):
        return ConversationHandler.END

    reason = (update.message.text or "").strip()
    if not reason or len(reason) > MAX_TEXT_LEN:
        await update.message.reply_text(
            f"Alasan harus diisi (1–{MAX_TEXT_LEN} karakter). Silakan ketik alasan Anda:"
        )
        return LEAVE_REASON

    context.user_data["leave_reason"] = reason
    prompt = (
        f"Alasan dicatat: <b>{_esc(reason)}</b>\n\n"
        f"👉 Langkah terakhir: Silakan kirimkan <b>foto bukti surat</b> (surat dokter / surat dispensasi sekolah):"
    )
    await update.message.reply_text(prompt, reply_markup=get_cancel_keyboard(), parse_mode="HTML")
    return LEAVE_PHOTO


async def leave_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private_message(update):
        return ConversationHandler.END

    photos = update.message.photo
    if not photos:
        await update.message.reply_text("Silakan kirimkan foto surat dokter atau surat dispensasi Anda:")
        return LEAVE_PHOTO

    user_id = update.effective_user.id
    is_admin = user_id in config.ADMIN_IDS
    today_str = utils.get_today_str()
    time_str = utils.get_current_time_str()

    db_user = database.get_user(user_id)
    if not db_user or db_user.get("status") != "approved":
        context.user_data.clear()
        await update.message.reply_text("⛔ Akun Anda tidak aktif / belum disetujui.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    leave_type = context.user_data.get("leave_type")
    reason = context.user_data.get("leave_reason")
    if not leave_type or not reason:
        context.user_data.clear()
        await update.message.reply_text(
            "⚠️ Sesi pengajuan izin tidak lengkap atau kadaluarsa. Silakan mulai ulang.",
            reply_markup=get_main_keyboard(is_admin),
        )
        return ConversationHandler.END

    photo = photos[-1]
    photo_file = await photo.get_file()

    filename = f"leave_{user_id}_{today_str}_{time_str.replace(':', '')}.jpg"
    filepath = config.PHOTOS_DIR / filename
    await photo_file.download_to_drive(custom_path=filepath)

    req_id = database.create_leave_request(
        telegram_id=user_id,
        date_str=today_str,
        leave_type=leave_type,
        reason=reason,
        photo_path=str(filepath),
    )

    if not req_id:
        filepath.unlink(missing_ok=True)
        await update.message.reply_text(
            "❌ Terjadi kesalahan sistem saat menyimpan pengajuan izin. Silakan coba lagi.",
            reply_markup=get_main_keyboard(is_admin),
        )
        context.user_data.clear()
        return ConversationHandler.END

    student_name = db_user.get("full_name", "Siswa")
    school = db_user.get("school", "-")
    nisn = db_user.get("nisn", "-")

    user_confirm = (
        f"🎉 <b>Pengajuan {_esc(leave_type)} Berhasil Dikirim!</b>\n\n"
        f"• Tanggal: <b>{_esc(today_str)}</b>\n"
        f"• Alasan: <b>{_esc(reason)}</b>\n\n"
        f"⏳ <i>Permintaan Anda telah diteruskan ke Admin untuk diverifikasi. Anda akan menerima notifikasi otomatis ketika disetujui.</i>"
    )
    await update.message.reply_text(user_confirm, reply_markup=get_main_keyboard(is_admin), parse_mode="HTML")

    admin_caption = (
        f"📋 <b>Permintaan Persetujuan {_esc(leave_type)} Siswa!</b>\n\n"
        f"👤 <b>Nama:</b> {_esc(student_name)}\n"
        f"🏫 <b>Sekolah:</b> {_esc(school)}\n"
        f"🆔 <b>NISN:</b> <code>{_esc(nisn)}</code>\n"
        f"📅 <b>Tanggal:</b> {_esc(today_str)}\n"
        f"📝 <b>Alasan:</b> {_esc(reason)}\n\n"
        f"Silakan periksa foto bukti di atas dan tentukan tindakan:"
    )
    admin_markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Setujui Izin", callback_data=f"leaveapp_{req_id}"),
            InlineKeyboardButton("❌ Tolak", callback_data=f"leaverej_{req_id}"),
        ]
    ])

    for admin_id in config.ADMIN_IDS:
        try:
            with open(filepath, "rb") as f:
                await context.bot.send_photo(
                    chat_id=admin_id,
                    photo=f,
                    caption=admin_caption,
                    reply_markup=admin_markup,
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.warning("Gagal kirim notif izin ke admin %s: %s", admin_id, e)

    context.user_data.clear()
    return ConversationHandler.END


async def leave_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menangani persetujuan atau penolakan pengajuan izin/sakit oleh Admin."""
    query = update.callback_query
    if not query or not update.effective_user or not query.message:
        return

    if query.message.chat.type != ChatType.PRIVATE:
        return

    admin_id = update.effective_user.id
    if admin_id not in config.ADMIN_IDS:
        await query.answer("⛔ Anda tidak memiliki hak akses admin.", show_alert=True)
        return

    await query.answer()

    data = query.data or ""
    if "_" not in data:
        await query.edit_message_caption(caption="❌ Format aksi tidak valid.")
        return

    action, req_id_str = data.split("_", 1)
    if action not in ("leaveapp", "leaverej"):
        await query.edit_message_caption(caption="❌ Aksi tidak dikenali.")
        return

    try:
        req_id = int(req_id_str)
    except ValueError:
        await query.edit_message_caption(caption="❌ ID pengajuan tidak valid.")
        return

    req = database.get_leave_request(req_id)
    if not req:
        await query.edit_message_caption(caption="❌ Data pengajuan izin tidak ditemukan.")
        return

    new_status = "approved" if action == "leaveapp" else "rejected"
    if not database.update_leave_request_status(req_id, new_status):
        await query.edit_message_caption(caption="ℹ️ Pengajuan izin ini sudah diproses sebelumnya oleh Admin.")
        return

    admin_name = update.effective_user.first_name or "Admin"
    student_id = req["telegram_id"]
    leave_type = req["leave_type"]
    date_str = req["date"]

    if action == "leaveapp":
        recorded = database.record_leave_attendance(student_id, date_str, leave_type, req.get("photo_path", ""))

        if recorded:
            sheets_sync.trigger_sheet_sync(
                full_name=req.get("full_name", "Siswa"),
                school=req.get("school", "-"),
                nisn=req.get("nisn", "-"),
                att_type=leave_type,
                office_name="-",
                date_str=date_str,
                time_str="-",
                distance=0,
                photo_path=req.get("photo_path", ""),
            )

        caption_edited = (
            f"✅ <b>Pengajuan {_esc(leave_type)} Telah Disetujui!</b>\n\n"
            f"👤 Siswa: <b>{_esc(req.get('full_name', '-'))}</b>\n"
            f"🏫 Sekolah: <b>{_esc(req.get('school', '-'))}</b>\n"
            f"📅 Tanggal: <b>{_esc(date_str)}</b>\n"
            f"📝 Alasan: <b>{_esc(req['reason'])}</b>\n"
            f"🛡️ Disetujui oleh: <b>{_esc(admin_name)}</b>"
        )
        await query.edit_message_caption(caption=caption_edited, parse_mode="HTML")

        try:
            student_msg = (
                f"✅ <b>Pengajuan {_esc(leave_type)} Anda Disetujui!</b>\n\n"
                f"📅 Tanggal: <b>{_esc(date_str)}</b>\n"
                f"Status kehadiran Anda hari ini telah dicatat sebagai <b>{_esc(leave_type)}</b>.\n"
                f"Semoga lekas sembuh / kegiatan Anda lancar!"
            )
            await context.bot.send_message(chat_id=student_id, text=student_msg, parse_mode="HTML")
        except Exception as e:
            logger.warning("Gagal kirim notif persetujuan izin ke user %s: %s", student_id, e)

    elif action == "leaverej":
        caption_edited = (
            f"❌ <b>Pengajuan {_esc(leave_type)} Ditolak!</b>\n\n"
            f"👤 Siswa: <b>{_esc(req.get('full_name', '-'))}</b>\n"
            f"🏫 Sekolah: <b>{_esc(req.get('school', '-'))}</b>\n"
            f"📅 Tanggal: <b>{_esc(date_str)}</b>\n"
            f"📝 Alasan: <b>{_esc(req['reason'])}</b>\n"
            f"🛡️ Ditolak oleh: <b>{_esc(admin_name)}</b>"
        )
        await query.edit_message_caption(caption=caption_edited, parse_mode="HTML")

        try:
            student_msg = (
                f"❌ <b>Pengajuan {_esc(leave_type)} Ditolak!</b>\n\n"
                f"Pengajuan Anda untuk tanggal <b>{_esc(date_str)}</b> ditolak oleh Admin.\n"
                f"Silakan hubungi pembimbing jika ada kekeliruan."
            )
            await context.bot.send_message(chat_id=student_id, text=student_msg, parse_mode="HTML")
        except Exception as e:
            logger.warning("Gagal kirim notif penolakan izin ke user %s: %s", student_id, e)


# ==========================================================
# 7. HANDLER MENU UMUM (RIWAYAT, PROFIL, REKAP ADMIN)
# ==========================================================
async def profile_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private_message(update):
        return

    user_id = update.effective_user.id
    db_user = database.get_user(user_id)
    is_admin = user_id in config.ADMIN_IDS

    if not db_user:
        await update.message.reply_text("⚠️ Anda belum terdaftar. Silakan ketik /start.")
        return

    user_status = db_user.get("status", "pending")
    if user_status != "approved":
        status_label = "Ditolak" if user_status == "rejected" else "Menunggu Approval Admin"
        await update.message.reply_text(
            f"👤 <b>Profil Pengguna</b>\n\n"
            f"• Nama: <b>{_esc(db_user.get('full_name', '-'))}</b>\n"
            f"• Status Akun: <b>{_esc(status_label)}</b>\n\n"
            f"ℹ️ Detail kantor dan jadwal tersedia setelah akun disetujui Admin.",
            parse_mode="HTML",
        )
        return

    today_str = utils.get_today_str()
    today_att = database.get_today_attendance(user_id, today_str)

    in_office = f" ({_esc(today_att['check_in_office'])})" if today_att and today_att.get("check_in_office") else ""
    out_office = f" ({_esc(today_att['check_out_office'])})" if today_att and today_att.get("check_out_office") else ""

    in_status = f"{_esc(today_att['check_in_time'])}{in_office}" if today_att and today_att.get("check_in_time") else "Belum"
    out_status = f"{_esc(today_att['check_out_time'])}{out_office}" if today_att and today_att.get("check_out_time") else "Belum"
    act_status = _esc(today_att.get("activity")) if today_att and today_att.get("activity") else "Belum diisi"

    office_list_text = "\n".join([f"• <b>{_esc(o['name'])}</b> (Radius {o['radius']:,.0f}m)" for o in config.OFFICES])

    now = utils.get_now()
    sched = utils.get_work_schedule(now)

    text = (
        f"👤 <b>Profil Pengguna</b>\n\n"
        f"• Nama: <b>{_esc(db_user['full_name'])}</b>\n"
        f"• Asal Sekolah: <b>{_esc(db_user.get('school', '-'))}</b>\n"
        f"• NISN: <code>{_esc(db_user.get('nisn', db_user.get('nik', '-')))}</code>\n"
        f"• ID Telegram: <code>{user_id}</code>\n"
        f"• Status Akun: <b>Disetujui (Approved)</b>\n"
        f"• Role: <b>{_esc(str(db_user.get('role', 'student')).capitalize())}</b>\n\n"
        f"⏰ <b>Ketentuan Jam Absensi Hari Ini:</b>\n"
        f"• Periode Masuk: <b>{_esc(sched['rule'])}</b>\n"
        f"• Batas Maksimal Masuk: <b>{_esc(sched['entry_time_str'])} WIB</b>\n"
        f"• Jam Buka Absen Pulang: <b>{config.CHECKOUT_OPEN_HOUR:02d}:{config.CHECKOUT_OPEN_MINUTE:02d} WIB</b>\n"
        f"• Batas Maksimal Cek Log Pulang: <b>23:59 WIB</b>\n\n"
        f"📌 <b>Status Absensi Hari Ini ({_esc(today_str)}):</b>\n"
        f"• Masuk: <b>{in_status}</b>\n"
        f"• Pulang: <b>{out_status}</b>\n"
        f"• Laporan Kegiatan: <b>{act_status}</b>\n\n"
        f"🏢 <b>Daftar Kantor Terdaftar:</b>\n"
        f"{office_list_text}"
    )
    await update.message.reply_text(text, reply_markup=get_main_keyboard(is_admin), parse_mode="HTML")


async def history_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private_message(update):
        return

    user_id = update.effective_user.id
    is_admin = user_id in config.ADMIN_IDS
    db_user = database.get_user(user_id)

    if not db_user:
        await update.message.reply_text("⚠️ Anda belum terdaftar. Silakan ketik /start.")
        return

    if db_user.get("status") != "approved":
        await update.message.reply_text("⏳ Akun Anda belum disetujui Admin.")
        return

    records = database.get_user_history(user_id, limit=7)

    if not records:
        await update.message.reply_text(
            "Belum ada riwayat absensi yang tercatat.",
            reply_markup=get_main_keyboard(is_admin),
        )
        return

    lines = ["📋 <b>Riwayat 7 Absensi Terakhir:</b>", ""]
    for r in records:
        in_t = _esc(r.get("check_in_time") or "-")
        in_off = f" ({_esc(r['check_in_office'])})" if r.get("check_in_office") else ""
        out_t = _esc(r.get("check_out_time") or "-")
        out_off = f" ({_esc(r['check_out_office'])})" if r.get("check_out_office") else ""
        lines.append(f"📅 <b>{_esc(r['date'])}</b>\n   Masuk: <code>{in_t}</code>{in_off}\n   Pulang: <code>{out_t}</code>{out_off}")

    await update.message.reply_text("\n".join(lines), reply_markup=get_main_keyboard(is_admin), parse_mode="HTML")


async def rekap_admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private_message(update):
        return

    user_id = update.effective_user.id
    if user_id not in config.ADMIN_IDS:
        await update.message.reply_text("⛔ Maaf, fitur ini hanya dapat diakses oleh Admin.")
        return

    records = database.get_all_attendance()
    if not records:
        await update.message.reply_text("Belum ada data absensi untuk direkap.")
        return

    csv_path = utils.generate_csv_report(records, filename_prefix="rekap_semua")

    await update.message.reply_text("⏳ Sedang menyiapkan file rekap absensi...")
    try:
        with open(csv_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=Path(csv_path).name,
                caption=f"📊 <b>Laporan Rekap Absensi</b>\nTotal data: {len(records)} baris.",
                parse_mode="HTML",
            )
    finally:
        Path(csv_path).unlink(missing_ok=True)


async def recap_daily_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Perintah Admin (/rekapharian) untuk meminta rekap kehadiran hari ini secara langsung.
    Mengirim langsung ke admin yang meminta tanpa mengganggu penjadwalan otomatis harian.
    """
    if not _is_private_message(update):
        return

    user_id = update.effective_user.id
    if user_id not in config.ADMIN_IDS:
        await update.message.reply_text("⛔ Perintah ini khusus untuk Admin.")
        return

    today_str = utils.get_today_str()
    await update.message.reply_text(f"⏳ Menyusun rekap absensi harian untuk <b>{_esc(today_str)}</b>...", parse_mode="HTML")
    sent = await recap.send_daily_recap(
        context.bot,
        date_str=today_str,
        force=True,
        mark_sent=False,
        recipient_ids=[user_id],
    )
    if sent == 0:
        await update.message.reply_text("⚠️ Gagal mengirim rekap harian.")


async def admin_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menangani aksi persetujuan (approval) atau penolakan siswa oleh Admin."""
    query = update.callback_query
    if not query or not update.effective_user or not query.message:
        return

    if query.message.chat.type != ChatType.PRIVATE:
        return

    admin_id = update.effective_user.id
    if admin_id not in config.ADMIN_IDS:
        await query.answer("⛔ Anda tidak memiliki hak akses admin.", show_alert=True)
        return

    await query.answer()

    data = query.data or ""
    if "_" not in data:
        await query.edit_message_text("❌ Format aksi tidak valid.")
        return

    action, target_user_id_str = data.split("_", 1)
    if action not in ("approve", "reject"):
        await query.edit_message_text("❌ Aksi tidak dikenali.")
        return

    try:
        target_user_id = int(target_user_id_str)
    except ValueError:
        await query.edit_message_text("❌ ID pengguna tidak valid.")
        return

    target_user = database.get_user(target_user_id)
    if not target_user:
        await query.edit_message_text("❌ Data pengguna tidak ditemukan di database.")
        return

    new_status = "approved" if action == "approve" else "rejected"
    if not database.update_user_status(target_user_id, new_status):
        await query.edit_message_text("ℹ️ Pendaftaran pengguna ini sudah diproses sebelumnya oleh Admin.")
        return

    admin_name = update.effective_user.first_name or "Admin"

    if action == "approve":
        text_edited = (
            f"✅ <b>Pendaftaran Disetujui!</b>\n\n"
            f"👤 <b>Siswa:</b> {_esc(target_user['full_name'])}\n"
            f"🏫 <b>Sekolah:</b> {_esc(target_user.get('school', '-'))}\n"
            f"🆔 <b>NISN:</b> <code>{_esc(target_user.get('nisn', '-'))}</code>\n"
            f"🛡️ <b>Disetujui oleh:</b> {_esc(admin_name)}"
        )
        await query.edit_message_text(text_edited, parse_mode="HTML")

        try:
            welcome_text = (
                f"🎉 <b>Selamat, {_esc(target_user['full_name'])}!</b>\n\n"
                f"Pendaftaran absensi Anda telah <b>DISETUJUI</b> oleh Admin.\n"
                f"Sekarang Anda sudah dapat melakukan absensi masuk dan pulang.\n\n"
                f"Silakan gunakan menu di bawah untuk mulai absensi."
            )
            await context.bot.send_message(
                chat_id=target_user_id,
                text=welcome_text,
                reply_markup=get_main_keyboard(target_user_id in config.ADMIN_IDS),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("Gagal kirim notif persetujuan ke user %s: %s", target_user_id, e)

    elif action == "reject":
        text_edited = (
            f"❌ <b>Pendaftaran Ditolak!</b>\n\n"
            f"👤 <b>Siswa:</b> {_esc(target_user['full_name'])}\n"
            f"🏫 <b>Sekolah:</b> {_esc(target_user.get('school', '-'))}\n"
            f"🆔 <b>NISN:</b> <code>{_esc(target_user.get('nisn', '-'))}</code>\n"
            f"🛡️ <b>Ditolak oleh:</b> {_esc(admin_name)}"
        )
        await query.edit_message_text(text_edited, parse_mode="HTML")

        try:
            reject_text = (
                f"Halo, <b>{_esc(target_user['full_name'])}</b>.\n\n"
                f"Pendaftaran absensi Anda <b>DITOLAK</b> oleh Admin.\n"
                f"Silakan hubungi pihak pembimbing/sekolah jika ada kekeliruan data."
            )
            await context.bot.send_message(
                chat_id=target_user_id,
                text=reject_text,
                reply_markup=ReplyKeyboardRemove(),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("Gagal kirim notif penolakan ke user %s: %s", target_user_id, e)


async def send_reminder_to_students(bot) -> int:
    """Mengirim pesan broadcast pengingat absensi ke semua siswa yang belum absen masuk."""
    now = utils.get_now()
    today_str = now.strftime("%Y-%m-%d")
    sched = utils.get_work_schedule(now)
    office_names = " atau ".join(_esc(o["name"]) for o in config.OFFICES) or "Kantor"

    students = database.get_approved_students()
    sent_count = 0

    for idx, student in enumerate(students):
        student_id = student["telegram_id"]
        att = database.get_today_attendance(student_id, today_str)
        is_already_handled = att and (
            att.get("status") in ("Hadir", "Sakit", "Izin", "Libur")
            or (att.get("check_in_time") and att.get("check_in_time") != "-")
        )
        if not is_already_handled:
            reminder_text = (
                f"⏰ <b>PENGINGAT ABSENSI MASUK</b>\n\n"
                f"Selamat pagi, <b>{_esc(student['full_name'])}</b>!\n\n"
                f"Hari ini batas maksimal absensi masuk adalah pukul <b>{_esc(sched['entry_time_str'])} WIB</b> (1 jam lagi).\n\n"
                f"📌 <b>Ketentuan Jam Masuk Hari Ini:</b>\n"
                f"• Periode: <b>{_esc(sched['rule'])}</b>\n"
                f"• Batas Akhir Masuk: <b>{_esc(sched['entry_time_str'])} WIB</b>\n"
                f"• Lokasi: {office_names}\n\n"
                f"⚠️ <b>PERINGATAN:</b> Jika melewati pukul <b>{_esc(sched['entry_time_str'])} WIB</b>, absensi masuk otomatis dikunci/ditolak sistem dan Anda harus melapor ke pembimbing.\n\n"
                f"Segera lakukan absensi masuk jika Anda sudah tiba di lokasi kantor!"
            )
            try:
                await _send_message_throttled(
                    bot,
                    chat_id=student_id,
                    text=reminder_text,
                    reply_markup=get_main_keyboard(False),
                    parse_mode="HTML",
                )
                sent_count += 1
            except Exception as e:
                logger.warning("Gagal mengirim pengingat ke siswa %s: %s", student_id, e)
            if idx < len(students) - 1:
                await asyncio.sleep(0.05)

    return sent_count


async def send_checkout_reminder_to_students(bot) -> int:
    """Mengirim pesan broadcast pengingat absen pulang ke siswa yang sudah masuk tapi belum pulang."""
    today_str = utils.get_today_str()
    students = database.get_approved_students()
    sent_count = 0

    for idx, student in enumerate(students):
        student_id = student["telegram_id"]
        att = database.get_today_attendance(student_id, today_str)

        has_checked_in = att and att.get("check_in_time") and att.get("check_in_time") != "-"
        has_checked_out = att and att.get("check_out_time") and att.get("check_out_time") != "-"
        is_day_off = att and att.get("status") in ("Libur", "Izin", "Sakit")

        if has_checked_in and not has_checked_out and not is_day_off:
            reminder_text = (
                f"⏰ <b>PENGINGAT CEK LOG PULANG &amp; LAPORAN KEGIATAN</b>\n\n"
                f"Halo, <b>{_esc(student['full_name'])}</b>!\n\n"
                f"Anda tercatat sudah melakukan <b>Absen Masuk</b> hari ini, namun <b>belum melakukan Absen Pulang</b>.\n\n"
                f"⚠️ <b>Batas Waktu Cek Log:</b> Batas maksimal untuk absen pulang dan mengirimkan laporan kegiatan adalah pukul <b>23:59 WIB (12 malam)</b>.\n\n"
                f"Silakan tekan tombol <b>'🔴 Absen Pulang'</b> sekarang agar kehadiran dan laporan kegiatan Anda tercatat lengkap!"
            )
            try:
                await _send_message_throttled(
                    bot,
                    chat_id=student_id,
                    text=reminder_text,
                    reply_markup=get_main_keyboard(False),
                    parse_mode="HTML",
                )
                sent_count += 1
            except Exception as e:
                logger.warning("Gagal mengirim pengingat pulang ke siswa %s: %s", student_id, e)
            if idx < len(students) - 1:
                await asyncio.sleep(0.05)

    return sent_count


async def run_scheduler_tick(bot, now: Optional[datetime] = None) -> Dict[str, int]:
    """
    Menjalankan satu siklus pengecekan jadwal otomatis:
    1. Pengingat masuk pagi (07:00 atau 08:00 sesuai tanggal)
    2. Pengingat pulang malam (20:00)
    3. Rekap absensi harian setelah jam pulang (RECAP_HOUR:RECAP_MINUTE)
    """
    async with _get_scheduler_lock():
        current_now = now or utils.get_now()
        today_str = current_now.strftime("%Y-%m-%d")
        sched = utils.get_work_schedule(current_now)

        results = {"morning_sent": 0, "evening_sent": 0, "recap_sent": 0}

        # 1. Pengingat jam masuk pagi
        if (
            current_now.hour == sched["reminder_hour"]
            and current_now.minute == sched["reminder_minute"]
            and not database.has_scheduled_job_run("morning_reminder", today_str)
        ):
            logger.info("Mengirim broadcast pengingat masuk otomatis (%s)...", sched["rule"])
            total_sent = await send_reminder_to_students(bot)
            database.mark_scheduled_job_run(
                "morning_reminder",
                today_str,
                current_now.strftime("%Y-%m-%d %H:%M:%S"),
                total_sent,
            )
            results["morning_sent"] = total_sent
            logger.info("Pengingat masuk otomatis berhasil dikirim ke %d siswa.", total_sent)

        # 2. Pengingat cek log pulang malam (20:00)
        if (
            current_now.hour == 20
            and current_now.minute == 0
            and not database.has_scheduled_job_run("evening_reminder", today_str)
        ):
            logger.info("Mengirim broadcast pengingat cek log pulang (20:00)...")
            total_checkout_sent = await send_checkout_reminder_to_students(bot)
            database.mark_scheduled_job_run(
                "evening_reminder",
                today_str,
                current_now.strftime("%Y-%m-%d %H:%M:%S"),
                total_checkout_sent,
            )
            results["evening_sent"] = total_checkout_sent
            logger.info("Pengingat pulang malam berhasil dikirim ke %d siswa.", total_checkout_sent)

        # 3. Rekap absensi harian otomatis setelah jam pulang
        already_recap_sent = database.has_scheduled_job_run(recap.DAILY_RECAP_JOB_NAME, today_str)
        if recap.is_recap_due(
            now=current_now,
            enabled=config.RECAP_ENABLED,
            hour=config.RECAP_HOUR,
            minute=config.RECAP_MINUTE,
            skip_weekends=config.RECAP_SKIP_WEEKENDS,
            already_sent=already_recap_sent,
        ):
            last_fail = _recap_last_failed_attempt.get(today_str)
            in_cooldown = (
                last_fail is not None
                and (current_now - last_fail).total_seconds() < _RECAP_FAILURE_COOLDOWN_SECONDS
            )
            if not in_cooldown:
                logger.info("Mengirim rekap absensi harian otomatis untuk tanggal %s...", today_str)
                recap_sent = await recap.send_daily_recap(
                    bot,
                    date_str=today_str,
                    force=False,
                    now=current_now,
                )
                results["recap_sent"] = recap_sent
                if recap_sent > 0:
                    _recap_last_failed_attempt.pop(today_str, None)
                    logger.info("Rekap absensi harian berhasil dikirim ke %d penerima.", recap_sent)
                else:
                    _recap_last_failed_attempt[today_str] = current_now
                    logger.warning("Rekap harian %s gagal dikirim ke semua penerima; masuk cooldown retry.", today_str)

        return results


async def reminder_scheduler_loop(bot):
    """Background loop yang berjalan setiap 30 detik untuk memicu run_scheduler_tick."""
    logger.info("Scheduler pengingat & rekap absensi otomatis aktif.")
    while True:
        try:
            await run_scheduler_tick(bot)
        except asyncio.CancelledError:
            logger.info("Scheduler pengingat & rekap dihentikan.")
            raise
        except Exception as e:
            logger.error("Error pada scheduler pengingat: %s", e)

        await asyncio.sleep(30)


async def broadcast_pengingat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Perintah manual bagi Admin untuk mengirimkan broadcast pengingat sekarang (/kirimpengingat)."""
    if not _is_private_message(update):
        return

    user_id = update.effective_user.id
    if user_id not in config.ADMIN_IDS:
        await update.message.reply_text("⛔ Perintah ini khusus untuk Admin.")
        return

    await update.message.reply_text("⏳ Sedang mengirimkan pengingat ke seluruh siswa yang belum absen masuk hari ini...")
    total_sent = await send_reminder_to_students(context.bot)
    await update.message.reply_text(
        f"✅ Selesai! Pengingat berhasil dikirim ke <b>{total_sent} siswa</b> yang belum absen masuk hari ini.",
        parse_mode="HTML",
    )


async def broadcast_pengingat_pulang_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Perintah manual bagi Admin untuk mengirimkan broadcast pengingat pulang sekarang (/kirimpengingatpulang)."""
    if not _is_private_message(update):
        return

    user_id = update.effective_user.id
    if user_id not in config.ADMIN_IDS:
        await update.message.reply_text("⛔ Perintah ini khusus untuk Admin.")
        return

    await update.message.reply_text("⏳ Sedang mengirimkan pengingat pulang ke siswa yang belum cek log pulang...")
    total_sent = await send_checkout_reminder_to_students(context.bot)
    await update.message.reply_text(
        f"✅ Selesai! Pengingat pulang berhasil dikirim ke <b>{total_sent} siswa</b> yang belum checkout hari ini.",
        parse_mode="HTML",
    )


async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Membatalkan alur percakapan dan membersihkan foto sementara jika ada."""
    if not _is_private_message(update):
        return ConversationHandler.END

    user_id = update.effective_user.id
    is_admin = user_id in config.ADMIN_IDS
    temp_photo = context.user_data.get("checkout_photo_path")
    if temp_photo:
        Path(temp_photo).unlink(missing_ok=True)
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Proses telah dibatalkan.",
        reply_markup=get_main_keyboard(is_admin),
    )
    return ConversationHandler.END


# ==========================================================
# 8. MAIN ENTRY POINT
# ==========================================================
def main():
    if not config.BOT_TOKEN or config.BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("\n" + "=" * 60)
        print("❌ ERROR: BOT_TOKEN belum diset di file .env!")
        print("1. Buka Telegram dan chat ke @BotFather untuk membuat bot baru.")
        print("2. Dapatkan API token bot Anda.")
        print("3. Buat file .env (salin dari .env.example) dan isi BOT_TOKEN Anda.")
        print("=" * 60 + "\n")
        sys.exit(1)

    database.init_db()
    logger.info("Database berhasil diinisialisasi.")
    logger.info("Daftar Kantor Terdaftar (%d kantor):", len(config.OFFICES))
    for o in config.OFFICES:
        logger.info(" - %s: (%s, %s) radius %sm", o["name"], o["lat"], o["lon"], o["radius"])

    async def on_startup(application: Application):
        task = asyncio.create_task(reminder_scheduler_loop(application.bot))
        application.bot_data["scheduler_task"] = task

    async def on_shutdown(application: Application):
        task = application.bot_data.get("scheduler_task")
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await sheets_sync.wait_pending_syncs(timeout=5.0)

    app = (
        ApplicationBuilder()
        .token(config.BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    private = filters.ChatType.PRIVATE

    cancel_handlers = [
        CommandHandler("cancel", cancel_handler, filters=private),
        CommandHandler("batal", cancel_handler, filters=private),
        MessageHandler(private & filters.Regex("^❌ Batal$"), cancel_handler),
    ]

    # 1. Conversation Handler Registrasi
    reg_conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_command, filters=private),
            CommandHandler("daftar", start_command, filters=private),
        ],
        states={
            REG_NAME: [
                *cancel_handlers,
                MessageHandler(private & filters.TEXT & ~filters.COMMAND, reg_name_handler),
            ],
            REG_SCHOOL: [
                *cancel_handlers,
                MessageHandler(private & filters.TEXT & ~filters.COMMAND, reg_school_handler),
            ],
            REG_NISN: [
                *cancel_handlers,
                MessageHandler(private & filters.TEXT & ~filters.COMMAND, reg_nisn_handler),
            ],
        },
        fallbacks=cancel_handlers,
        conversation_timeout=CONVERSATION_TIMEOUT_SECONDS,
    )

    # 2. Conversation Handler Absen Masuk
    checkin_conv = ConversationHandler(
        entry_points=[
            MessageHandler(private & filters.Regex("^🟢 Absen Masuk$"), checkin_start),
            CommandHandler("masuk", checkin_start, filters=private),
        ],
        states={
            CHECKIN_LOC: [
                *cancel_handlers,
                MessageHandler(private & filters.LOCATION, checkin_location_received),
            ],
            CHECKIN_PHOTO: [
                *cancel_handlers,
                MessageHandler(private & filters.PHOTO, checkin_photo_received),
            ],
        },
        fallbacks=cancel_handlers,
        conversation_timeout=CONVERSATION_TIMEOUT_SECONDS,
    )

    # 3. Conversation Handler Absen Pulang
    checkout_conv = ConversationHandler(
        entry_points=[
            MessageHandler(private & filters.Regex("^🔴 Absen Pulang$"), checkout_start),
            CommandHandler("pulang", checkout_start, filters=private),
        ],
        states={
            CHECKOUT_LOC: [
                *cancel_handlers,
                MessageHandler(private & filters.LOCATION, checkout_location_received),
            ],
            CHECKOUT_PHOTO: [
                *cancel_handlers,
                MessageHandler(private & filters.PHOTO, checkout_photo_received),
            ],
            CHECKOUT_ACTIVITY: [
                *cancel_handlers,
                MessageHandler(private & filters.TEXT & ~filters.COMMAND, checkout_activity_received),
            ],
        },
        fallbacks=cancel_handlers,
        conversation_timeout=CONVERSATION_TIMEOUT_SECONDS,
    )

    # 4. Conversation Handler Pengajuan Izin / Sakit
    leave_conv = ConversationHandler(
        entry_points=[
            MessageHandler(private & filters.Regex("^📄 Ajukan Izin / Sakit$"), leave_start),
            CommandHandler("izin", leave_start, filters=private),
        ],
        states={
            LEAVE_TYPE: [
                *cancel_handlers,
                MessageHandler(private & filters.TEXT & ~filters.COMMAND, leave_type_received),
            ],
            LEAVE_REASON: [
                *cancel_handlers,
                MessageHandler(private & filters.TEXT & ~filters.COMMAND, leave_reason_received),
            ],
            LEAVE_PHOTO: [
                *cancel_handlers,
                MessageHandler(private & filters.PHOTO, leave_photo_received),
            ],
        },
        fallbacks=cancel_handlers,
        conversation_timeout=CONVERSATION_TIMEOUT_SECONDS,
    )

    # 5. Conversation Handler Laporan Kegiatan Harian
    activity_conv = ConversationHandler(
        entry_points=[
            MessageHandler(private & filters.Regex("^📝 Laporan Kegiatan$"), activity_report_start),
            CommandHandler("lapor", activity_report_start, filters=private),
        ],
        states={
            ACTIVITY_TEXT: [
                *cancel_handlers,
                MessageHandler(private & filters.TEXT & ~filters.COMMAND, activity_report_received),
            ]
        },
        fallbacks=cancel_handlers,
        conversation_timeout=CONVERSATION_TIMEOUT_SECONDS,
    )

    # Daftarkan handlers (semua terbatas ke chat privat)
    app.add_handler(reg_conv)
    app.add_handler(checkin_conv)
    app.add_handler(checkout_conv)
    app.add_handler(leave_conv)
    app.add_handler(activity_conv)
    app.add_handler(CallbackQueryHandler(admin_approval_callback, pattern="^(approve|reject)_"))
    app.add_handler(CallbackQueryHandler(leave_approval_callback, pattern="^(leaveapp|leaverej)_"))

    # Handler Libur Akhir Pekan (Sabtu & Minggu)
    app.add_handler(MessageHandler(private & filters.Regex("^🏖️ Libur Hari Ini$"), day_off_handler))
    app.add_handler(CommandHandler("libur", day_off_handler, filters=private))

    # Menu statis & perintah admin (tanpa duplikasi handler ConversationHandler)
    app.add_handler(MessageHandler(private & filters.Regex("^👤 Profil Saya$"), profile_handler))
    app.add_handler(MessageHandler(private & filters.Regex("^📋 Riwayat Absensi$"), history_handler))
    app.add_handler(MessageHandler(private & filters.Regex("^📊 Rekap Absensi \\(Admin\\)$"), rekap_admin_handler))
    app.add_handler(CommandHandler("rekap", rekap_admin_handler, filters=private))
    app.add_handler(CommandHandler("rekapharian", recap_daily_command, filters=private))
    app.add_handler(CommandHandler("kirimpengingat", broadcast_pengingat_command, filters=private))
    app.add_handler(CommandHandler("kirimpengingatpulang", broadcast_pengingat_pulang_command, filters=private))
    app.add_handler(CommandHandler("profil", profile_handler, filters=private))
    app.add_handler(CommandHandler("riwayat", history_handler, filters=private))

    logger.info("Bot absensi Telegram siap dijalankan!")
    app.run_polling()


if __name__ == "__main__":
    main()
