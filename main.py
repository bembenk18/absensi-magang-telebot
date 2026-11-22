import asyncio
import logging
import sys
from pathlib import Path

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    Application,
)

import config
import database
import utils
import sheets_sync

# Setup logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# State untuk ConversationHandler
REG_NAME, REG_SCHOOL, REG_NISN = range(3)
CHECKIN_LOC, CHECKIN_PHOTO = range(3, 5)
CHECKOUT_LOC, CHECKOUT_PHOTO, CHECKOUT_ACTIVITY = range(5, 8)
LEAVE_TYPE, LEAVE_REASON, LEAVE_PHOTO = range(8, 11)
ACTIVITY_TEXT = 11

def get_main_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """Membuat keyboard menu utama."""
    now = utils.get_now()
    is_weekend = now.weekday() in (5, 6) # Sabtu (5) atau Minggu (6)

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
        [KeyboardButton("❌ Batal")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ==========================================================
# 1. HANDLER PENDAFTARAN & START
# ==========================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = database.get_user(user.id)
    is_admin = user.id in config.ADMIN_IDS

    if db_user:
        user_status = db_user.get("status", "pending")
        if user_status == "pending":
            pending_text = (
                f"👋 Halo, *{db_user['full_name']}*!\n\n"
                f"⏳ Pendaftaran Anda saat ini **Menunggu Persetujuan (Approval) Admin**.\n\n"
                f"• Asal Sekolah: *{db_user.get('school', '-')}*\n"
                f"• NISN: `{db_user.get('nisn', '-')}`\n\n"
                f"📌 Anda akan menerima notifikasi otomatis di sini segera setelah disetujui oleh Admin."
            )
            await update.message.reply_text(pending_text, parse_mode="Markdown")
            return ConversationHandler.END
        elif user_status == "rejected":
            rejected_text = (
                f"👋 Halo, *{db_user['full_name']}*.\n\n"
                f"⛔ Pendaftaran Anda telah **Ditolak** oleh Admin.\n"
                f"Silakan hubungi pihak pembimbing/sekolah jika ada kesalahan data."
            )
            await update.message.reply_text(rejected_text, parse_mode="Markdown")
            return ConversationHandler.END

        welcome_text = (
            f"👋 Halo, *{db_user['full_name']}*!\n\n"
            f"Selamat datang kembali di *Sistem Absensi Telegram*.\n"
            f"Silakan pilih menu di bawah ini untuk memulai absensi."
        )
        await update.message.reply_text(
            welcome_text,
            reply_markup=get_main_keyboard(is_admin),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    # Jika belum terdaftar, mulai proses registrasi
    intro_text = (
        f"👋 Halo, *{user.first_name}*!\n\n"
        f"Selamat datang di *Sistem Absensi Prakerin/Magang*.\n"
        f"Anda belum terdaftar dalam sistem. Mari lengkapi data pendaftaran Anda.\n\n"
        f"👉 Langkah 1: Silakan ketik **Nama Lengkap** Anda:"
    )
    await update.message.reply_text(intro_text, reply_markup=get_cancel_keyboard(), parse_mode="Markdown")
    return REG_NAME

async def reg_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Nama tidak boleh kosong. Silakan ketik Nama Lengkap Anda:")
        return REG_NAME

    context.user_data["reg_name"] = name
    await update.message.reply_text(
        f"Terima kasih, *{name}*.\n\n👉 Langkah 2: Masukkan **Asal Sekolah / Institusi** Anda (contoh: *SMKN 1 Jombang*):",
        reply_markup=get_cancel_keyboard(),
        parse_mode="Markdown"
    )
    return REG_SCHOOL

async def reg_school_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    school = update.message.text.strip()
    if not school:
        await update.message.reply_text("Asal sekolah tidak boleh kosong. Silakan ketik Asal Sekolah Anda:")
        return REG_SCHOOL

    context.user_data["reg_school"] = school
    await update.message.reply_text(
        f"Baik, sekolah Anda: *{school}*.\n\n👉 Langkah 3: Sekarang masukkan **NISN** (Nomor Induk Siswa Nasional) Anda:",
        reply_markup=get_cancel_keyboard(),
        parse_mode="Markdown"
    )
    return REG_NISN

async def reg_nisn_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nisn = update.message.text.strip()
    name = context.user_data.get("reg_name", "Siswa")
    school = context.user_data.get("reg_school", "-")
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
        status=status
    )

    if success:
        if is_admin:
            text = (
                f"🎉 *Pendaftaran Admin Berhasil!*\n\n"
                f"👤 **Nama:** {name}\n"
                f"🏫 **Sekolah:** {school}\n"
                f"🆔 **NISN:** {nisn}\n"
                f"🔰 **Role:** Admin (Auto-Approved)\n\n"
                f"Anda dapat menggunakan seluruh menu absensi dan admin."
            )
            await update.message.reply_text(text, reply_markup=get_main_keyboard(True), parse_mode="Markdown")
        else:
            text = (
                f"🎉 *Pendaftaran Berhasil Dikirim!*\n\n"
                f"📋 **Rincian Data Anda:**\n"
                f"• Nama: *{name}*\n"
                f"• Asal Sekolah: *{school}*\n"
                f"• NISN: `{nisn}`\n\n"
                f"⏳ _Pendaftaran Anda telah diteruskan ke Admin untuk diverifikasi._\n"
                f"Anda akan menerima pemberitahuan otomatis di sini setelah akun Anda disetujui (Approved)."
            )
            await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")

            # Kirim notifikasi instan ke seluruh Admin
            admin_notify_text = (
                f"🔔 *Permintaan Pendaftaran Siswa Baru!*\n\n"
                f"👤 **Nama:** {name}\n"
                f"🏫 **Asal Sekolah:** {school}\n"
                f"🆔 **NISN:** `{nisn}`\n"
                f"🆔 **ID Telegram:** `{user_id}`\n\n"
                f"Silakan periksa dan pilih tindakan di bawah:"
            )
            admin_markup = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Setujui", callback_data=f"approve_{user_id}"),
                    InlineKeyboardButton("❌ Tolak", callback_data=f"reject_{user_id}")
                ]
            ])
            for admin_id in config.ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=admin_notify_text,
                        reply_markup=admin_markup,
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.warning("Gagal kirim notif ke admin %s: %s", admin_id, e)
    else:
        await update.message.reply_text(
            "❌ Terjadi kesalahan saat menyimpan pendaftaran. Silakan coba kirim /start lagi.",
            reply_markup=ReplyKeyboardRemove()
        )

    context.user_data.clear()
    return ConversationHandler.END

# ==========================================================
# 2. HANDLER ABSEN MASUK
# ==========================================================
async def checkin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_user = database.get_user(user_id)
    is_admin = user_id in config.ADMIN_IDS

    if not db_user:
        await update.message.reply_text(
            "⚠️ Anda belum terdaftar. Silakan ketik /start untuk mendaftar.",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    user_status = db_user.get("status", "pending")
    if user_status == "pending":
        await update.message.reply_text(
            "⏳ Pendaftaran Anda masih **Menunggu Persetujuan (Approval) Admin**.\n"
            "Anda belum dapat melakukan absensi sebelum disetujui.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END
    elif user_status == "rejected":
        await update.message.reply_text(
            "⛔ Pendaftaran Anda telah **Ditolak** oleh Admin.\n"
            "Silakan hubungi pihak pembimbing/sekolah.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    # Validasi batas maksimal jam masuk
    is_late, entry_time_str, cur_time = utils.is_past_entry_cutoff()
    if is_late:
        late_text = (
            "❌ *Absen Masuk Ditolak!*\n\n"
            f"Batas waktu absensi masuk hari ini telah lewat.\n"
            f"• Batas Maksimal: *{entry_time_str} WIB*\n"
            f"• Waktu Saat Ini: *{cur_time} WIB*\n\n"
            "⚠️ Anda tidak diperkenankan absen karena telah melewati batas jam masuk.\n"
            "Silakan segera **hubungi pembimbing** untuk konfirmasi keterlambatan Anda."
        )
        await update.message.reply_text(late_text, reply_markup=get_main_keyboard(is_admin), parse_mode="Markdown")
        return ConversationHandler.END

    today_str = utils.get_today_str()
    attendance = database.get_today_attendance(user_id, today_str)

    if attendance and attendance.get("check_in_time"):
        jam_masuk = attendance["check_in_time"]
        office_name = attendance.get("check_in_office") or "Kantor"
        await update.message.reply_text(
            f"ℹ️ Anda sudah melakukan *Absen Masuk* hari ini di *{office_name}* pada pukul *{jam_masuk} WIB*.\n"
            f"Jangan lupa untuk *Absen Pulang* nanti setelah selesai bekerja ya!",
            reply_markup=get_main_keyboard(is_admin),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    text = (
        "🟢 **Proses Absen Masuk**\n\n"
        "Langkah 1: Silakan bagikan lokasi GPS Anda saat ini dengan menekan tombol **'📍 Kirim Lokasi Saat Ini'** di bawah."
    )
    await update.message.reply_text(text, reply_markup=get_location_keyboard(), parse_mode="Markdown")
    return CHECKIN_LOC

async def checkin_location_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_admin = user_id in config.ADMIN_IDS

    is_late, entry_time_str, cur_time = utils.is_past_entry_cutoff()
    if is_late:
        await update.message.reply_text(
            f"❌ *Absen Masuk Ditolak!*\n\nBatas maksimal jam masuk ({entry_time_str} WIB) telah lewat.\nSilakan segera **hubungi pembimbing**.",
            reply_markup=get_main_keyboard(is_admin),
            parse_mode="Markdown"
        )
        context.user_data.clear()
        return ConversationHandler.END

    location = update.message.location
    if not location:
        await update.message.reply_text(
            "Mohon tekan tombol '📍 Kirim Lokasi Saat Ini' untuk membagikan koordinat GPS Anda.",
            reply_markup=get_location_keyboard()
        )
        return CHECKIN_LOC

    lat = location.latitude
    lon = location.longitude
    user_id = update.effective_user.id
    is_admin = user_id in config.ADMIN_IDS

    check_res = utils.check_proximity_all_offices(lat, lon, config.OFFICES)

    if not check_res["is_valid"]:
        lines = [
            "❌ *Absen Masuk Ditolak!*\n",
            "Anda berada di luar radius kantor yang diizinkan:\n"
        ]
        for d in check_res["details"]:
            radius_text = f"{d['radius']:,.0f} meter" if d["radius"] > 0 else "Bebas"
            lines.append(f"• *{d['name']}*: Jarak {d['distance']:,.0f} m (Maks: {radius_text})")
        lines.append("\nSilakan pastikan GPS akurat dan Anda sudah berada di salah satu lokasi kantor.")

        await update.message.reply_text("\n".join(lines), reply_markup=get_main_keyboard(is_admin), parse_mode="Markdown")
        context.user_data.clear()
        return ConversationHandler.END

    matched_office = check_res["matched_office"]
    office_name = matched_office["name"]
    distance = check_res["distance"]

    # Simpan data lokasi sementara
    context.user_data["checkin_lat"] = lat
    context.user_data["checkin_lon"] = lon
    context.user_data["checkin_distance"] = distance
    context.user_data["checkin_office"] = office_name

    text = (
        f"✅ *Lokasi Terverifikasi di {office_name}!* 🏢\n"
        f"📍 Jarak dari titik kantor: *{distance:,.0f} meter*\n\n"
        f"Langkah 2: Sekarang silakan **kirimkan foto selfie Anda** sebagai bukti kehadiran:"
    )
    await update.message.reply_text(text, reply_markup=get_cancel_keyboard(), parse_mode="Markdown")
    return CHECKIN_PHOTO

async def checkin_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photos = update.message.photo
    if not photos:
        await update.message.reply_text(
            "Silakan kirimkan foto selfie langsung dari kamera Anda.",
            reply_markup=get_cancel_keyboard()
        )
        return CHECKIN_PHOTO

    user_id = update.effective_user.id
    is_admin = user_id in config.ADMIN_IDS
    today_str = utils.get_today_str()
    time_str = utils.get_current_time_str()

    photo = photos[-1]
    photo_file = await photo.get_file()

    filename = f"checkin_{user_id}_{today_str}_{time_str.replace(':', '')}.jpg"
    filepath = config.PHOTOS_DIR / filename
    await photo_file.download_to_drive(custom_path=filepath)

    lat = context.user_data.get("checkin_lat", 0.0)
    lon = context.user_data.get("checkin_lon", 0.0)
    distance = context.user_data.get("checkin_distance", 0.0)
    office_name = context.user_data.get("checkin_office", "Kantor")

    success = database.record_check_in(
        telegram_id=user_id,
        date_str=today_str,
        time_str=time_str,
        lat=lat,
        lon=lon,
        distance=distance,
        photo_path=str(filepath),
        office_name=office_name,
        status="Hadir"
    )

    if success:
        # Sinkronisasi ke Google Sheets & Google Drive
        db_u = database.get_user(user_id)
        sheets_sync.trigger_sheet_sync(
            full_name=db_u.get("full_name", "Siswa") if db_u else "Siswa",
            school=db_u.get("school", "-") if db_u else "-",
            nisn=db_u.get("nisn", "-") if db_u else "-",
            att_type="Masuk",
            office_name=office_name,
            date_str=today_str,
            time_str=time_str,
            distance=distance,
            photo_path=str(filepath)
        )

        text = (
            f"🎉 *Absen Masuk Berhasil!*\n\n"
            f"🏢 Lokasi: *{office_name}*\n"
            f"📅 Tanggal: *{today_str}*\n"
            f"🕒 Jam Masuk: *{time_str} WIB*\n"
            f"📍 Jarak: *{distance:,.0f} meter*\n\n"
            f"Selamat bertugas dan semoga hari Anda produktif!"
        )
    else:
        text = "❌ Terjadi kegagalan sistem saat mencatat absensi. Silakan coba lagi."

    await update.message.reply_text(text, reply_markup=get_main_keyboard(is_admin), parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

# ==========================================================
# 3. HANDLER ABSEN PULANG
# ==========================================================
async def checkout_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_user = database.get_user(user_id)
    is_admin = user_id in config.ADMIN_IDS

    if not db_user:
        await update.message.reply_text("⚠️ Anda belum terdaftar. Silakan ketik /start untuk mendaftar.")
        return ConversationHandler.END

    user_status = db_user.get("status", "pending")
    if user_status == "pending":
        await update.message.reply_text(
            "⏳ Pendaftaran Anda masih **Menunggu Persetujuan (Approval) Admin**.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END
    elif user_status == "rejected":
        await update.message.reply_text(
            "⛔ Pendaftaran Anda telah **Ditolak** oleh Admin.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    today_str = utils.get_today_str()
    attendance = database.get_today_attendance(user_id, today_str)

    if not attendance or not attendance.get("check_in_time"):
        await update.message.reply_text(
            "⚠️ Anda belum melakukan *Absen Masuk* hari ini.\n\n"
            "ℹ️ *Ketentuan Cek Log Pulang:*\n"
            "• Absen pulang hanya dapat dilakukan pada hari yang sama setelah absen masuk.\n"
            f"• Absen pulang dibuka mulai pukul *{config.CHECKOUT_OPEN_HOUR:02d}:{config.CHECKOUT_OPEN_MINUTE:02d} WIB* (jam 5 sore) hingga maksimal *23:59 WIB* (12 malam).",
            reply_markup=get_main_keyboard(is_admin),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    if attendance.get("check_out_time"):
        jam_pulang = attendance["check_out_time"]
        office_name = attendance.get("check_out_office") or "Kantor"
        await update.message.reply_text(
            f"ℹ️ Anda sudah melakukan *Absen Pulang* hari ini di *{office_name}* pada pukul *{jam_pulang} WIB*.\n"
            f"Sampai jumpa besok!",
            reply_markup=get_main_keyboard(is_admin),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    # Validasi jam buka absen pulang (Mulai 17:00 WIB)
    is_open, open_time_str, cur_time_str = utils.is_checkout_open()
    if not is_open:
        if not is_admin:
            await update.message.reply_text(
                f"⏳ *Absen Pulang Belum Dibuka!*\n\n"
                f"Absen Pulang baru dibuka mulai pukul *{open_time_str} WIB* (jam 5 sore) "
                f"hingga maksimal pukul *23:59 WIB* (12 malam).\n\n"
                f"🕒 Waktu saat ini: `{cur_time_str} WIB`\n"
                f"Silakan kembali lagi nanti setelah jam kerja selesai ya!",
                reply_markup=get_main_keyboard(is_admin),
                parse_mode="Markdown"
            )
            return ConversationHandler.END
        else:
            await update.message.reply_text(
                f"ℹ️ *[Mode Admin]* Saat ini pukul `{cur_time_str} WIB` (sebelum jam buka {open_time_str} WIB).\n"
                f"Akses admin mengizinkan untuk melanjutkan pengujian absen pulang.",
                parse_mode="Markdown"
            )

    text = (
        "🔴 **Proses Absen Pulang**\n\n"
        "Langkah 1: Silakan bagikan lokasi GPS Anda dengan menekan tombol **'📍 Kirim Lokasi Saat Ini'** di bawah."
    )
    await update.message.reply_text(text, reply_markup=get_location_keyboard(), parse_mode="Markdown")
    return CHECKOUT_LOC

async def checkout_location_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    location = update.message.location
    if not location:
        await update.message.reply_text(
            "Mohon tekan tombol '📍 Kirim Lokasi Saat Ini' untuk membagikan koordinat GPS Anda.",
            reply_markup=get_location_keyboard()
        )
        return CHECKOUT_LOC

    lat = location.latitude
    lon = location.longitude
    user_id = update.effective_user.id
    is_admin = user_id in config.ADMIN_IDS

    check_res = utils.check_proximity_all_offices(lat, lon, config.OFFICES)

    if not check_res["is_valid"]:
        lines = [
            "❌ *Absen Pulang Ditolak!*\n",
            "Anda berada di luar radius kantor yang diizinkan:\n"
        ]
        for d in check_res["details"]:
            radius_text = f"{d['radius']:,.0f} meter" if d["radius"] > 0 else "Bebas"
            lines.append(f"• *{d['name']}*: Jarak {d['distance']:,.0f} m (Maks: {radius_text})")
        lines.append("\nSilakan pastikan Anda berada di salah satu lokasi kantor.")

        await update.message.reply_text("\n".join(lines), reply_markup=get_main_keyboard(is_admin), parse_mode="Markdown")
        context.user_data.clear()
        return ConversationHandler.END

    matched_office = check_res["matched_office"]
    office_name = matched_office["name"]
    distance = check_res["distance"]

    context.user_data["checkout_lat"] = lat
    context.user_data["checkout_lon"] = lon
    context.user_data["checkout_distance"] = distance
    context.user_data["checkout_office"] = office_name

    text = (
        f"✅ *Lokasi Terverifikasi di {office_name}!* 🏢\n"
        f"📍 Jarak dari titik kantor: *{distance:,.0f} meter*\n\n"
        f"Langkah 2: Sekarang silakan **kirimkan foto selfie Anda** sebagai bukti absensi pulang:"
    )
    await update.message.reply_text(text, reply_markup=get_cancel_keyboard(), parse_mode="Markdown")
    return CHECKOUT_PHOTO

async def checkout_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photos = update.message.photo
    if not photos:
        await update.message.reply_text("Silakan kirimkan foto selfie Anda.", reply_markup=get_cancel_keyboard())
        return CHECKOUT_PHOTO

    user_id = update.effective_user.id
    is_admin = user_id in config.ADMIN_IDS
    today_str = utils.get_today_str()
    time_str = utils.get_current_time_str()

    photo = photos[-1]
    photo_file = await photo.get_file()

    filename = f"checkout_{user_id}_{today_str}_{time_str.replace(':', '')}.jpg"
    filepath = config.PHOTOS_DIR / filename
    await photo_file.download_to_drive(custom_path=filepath)

    context.user_data["checkout_photo_path"] = str(filepath)

    text = (
        "📸 *Foto Selfie Berhasil Diterima!*\n\n"
        "📝 **Langkah Terakhir: Laporan Kegiatan Harian**\n\n"
        "Silakan tuliskan ringkasan kegiatan / pekerjaan magang yang Anda lakukan seharian ini:\n"
        "_(Contoh: 1. Merapikan kabel jaringan, 2. Backup database harian, 3. Input berkas inventaris)_"
    )
    await update.message.reply_text(text, reply_markup=get_cancel_keyboard(), parse_mode="Markdown")
    return CHECKOUT_ACTIVITY

async def checkout_activity_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    activity_text = update.message.text.strip() if update.message.text else ""
    if not activity_text:
        await update.message.reply_text(
            "Mohon ketikkan ringkasan kegiatan Anda hari ini:",
            reply_markup=get_cancel_keyboard()
        )
        return CHECKOUT_ACTIVITY

    user_id = update.effective_user.id
    is_admin = user_id in config.ADMIN_IDS
    today_str = utils.get_today_str()
    time_str = utils.get_current_time_str()

    lat = context.user_data.get("checkout_lat", 0.0)
    lon = context.user_data.get("checkout_lon", 0.0)
    distance = context.user_data.get("checkout_distance", 0.0)
    office_name = context.user_data.get("checkout_office", "Kantor")
    filepath = context.user_data.get("checkout_photo_path", "")

    success = database.record_check_out(
        telegram_id=user_id,
        date_str=today_str,
        time_str=time_str,
        lat=lat,
        lon=lon,
        distance=distance,
        photo_path=filepath,
        office_name=office_name,
        activity=activity_text
    )

    if success:
        # Sinkronisasi ke Google Sheets & Google Drive
        db_u = database.get_user(user_id)
        sheets_sync.trigger_sheet_sync(
            full_name=db_u.get("full_name", "Siswa") if db_u else "Siswa",
            school=db_u.get("school", "-") if db_u else "-",
            nisn=db_u.get("nisn", "-") if db_u else "-",
            att_type="Pulang",
            office_name=office_name,
            date_str=today_str,
            time_str=time_str,
            distance=distance,
            photo_path=filepath,
            activity=activity_text
        )

        text = (
            f"🎉 *Absen Pulang & Laporan Kegiatan Berhasil!*\n\n"
            f"🏢 Lokasi: *{office_name}*\n"
            f"📅 Tanggal: *{today_str}*\n"
            f"🕒 Jam Pulang: *{time_str} WIB*\n"
            f"📍 Jarak Kantor: *{distance:,.0f} meter*\n"
            f"📝 *Laporan Kegiatan:*\n_{activity_text}_\n\n"
            f"Terima kasih atas kerja keras Anda hari ini. Selamat beristirahat!"
        )
    else:
        text = "❌ Terjadi kegagalan sistem saat mencatat absensi pulang."

    await update.message.reply_text(text, reply_markup=get_main_keyboard(is_admin), parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

# ==========================================================
# 4. HANDLER LAPORAN KEGIATAN HARIAN (MANDIRI)
# ==========================================================
async def activity_report_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Memulai proses pengisian atau pembaruan laporan kegiatan harian."""
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
            "⚠️ Anda belum melakukan *Absen Masuk* hari ini.\n"
            "Laporan kegiatan hanya dapat diisi jika Anda telah hadir bekerja.",
            reply_markup=get_main_keyboard(is_admin),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    current_act = att.get("activity")
    if current_act:
        prompt = (
            f"📝 *Laporan Kegiatan Hari Ini ({today_str}):*\n\n"
            f"_{current_act}_\n\n"
            f"✏️ *Ingin memperbarui isi laporan kegiatan?*\n"
            f"Silakan ketikkan teks laporan baru di bawah, atau tekan **❌ Batal** jika tidak ingin mengubah."
        )
    else:
        prompt = (
            f"📝 *Input Laporan Kegiatan Hari Ini ({today_str})*\n\n"
            f"Silakan ketikkan rangkuman kegiatan / pekerjaan yang Anda kerjakan hari ini:\n"
            f"_(Contoh: 1. Merapikan kabel jaringan, 2. Backup database harian, 3. Input berkas inventaris)_"
        )

    await update.message.reply_text(prompt, reply_markup=get_cancel_keyboard(), parse_mode="Markdown")
    return ACTIVITY_TEXT

async def activity_report_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    activity_text = update.message.text.strip() if update.message.text else ""
    if not activity_text:
        await update.message.reply_text("Mohon ketikkan laporan kegiatan Anda:", reply_markup=get_cancel_keyboard())
        return ACTIVITY_TEXT

    user_id = update.effective_user.id
    is_admin = user_id in config.ADMIN_IDS
    today_str = utils.get_today_str()
    time_str = utils.get_current_time_str()

    success = database.update_activity_report(user_id, today_str, activity_text)
    if success:
        db_user = database.get_user(user_id)
        att = database.get_today_attendance(user_id, today_str)
        # Update / kirim ke Google Sheets
        sheets_sync.trigger_sheet_sync(
            full_name=db_user.get("full_name", "Siswa") if db_user else "Siswa",
            school=db_user.get("school", "-") if db_user else "-",
            nisn=db_user.get("nisn", "-") if db_user else "-",
            att_type="Laporan",
            office_name=att.get("check_in_office", "-") if att else "-",
            date_str=today_str,
            time_str=time_str,
            distance=0,
            photo_path="",
            activity=activity_text
        )

        await update.message.reply_text(
            f"✅ *Laporan Kegiatan Berhasil Disimpan!*\n\n"
            f"📅 Tanggal: *{today_str}*\n"
            f"📝 *Kegiatan:*\n_{activity_text}_\n\n"
            f"Laporan kegiatan telah tercatat di sistem & Google Sheets.",
            reply_markup=get_main_keyboard(is_admin),
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "❌ Gagal menyimpan laporan kegiatan. Pastikan Anda sudah absen masuk hari ini.",
            reply_markup=get_main_keyboard(is_admin)
        )

    return ConversationHandler.END

# ==========================================================
# 4. HANDLER LIBUR AKHIR PEKAN (SABTU & MINGGU)
# ==========================================================
async def day_off_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Konfirmasi libur mandiri oleh siswa pada hari Sabtu atau Minggu."""
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
    if now.weekday() not in (5, 6): # Hanya Sabtu (5) dan Minggu (6)
        await update.message.reply_text(
            "⚠️ Tombol *Libur Hari Ini* hanya dapat digunakan pada hari Sabtu dan Minggu.\n"
            "Untuk hari kerja biasa (Senin - Jumat), silakan gunakan menu **'📄 Ajukan Izin / Sakit'** jika Anda berhalangan hadir.",
            parse_mode="Markdown"
        )
        return

    today_str = utils.get_today_str()
    att = database.get_today_attendance(user_id, today_str)

    if att:
        if att.get("status") == "Libur":
            await update.message.reply_text("ℹ️ Status kehadiran Anda hari ini sudah tercatat sebagai **Libur**.")
            return
        elif att.get("check_in_time") and att.get("check_in_time") != "-":
            await update.message.reply_text(
                f"⚠️ Anda sudah melakukan absen masuk hari ini pada pukul {att['check_in_time']} WIB."
            )
            return

    # Catat ke Database lokal
    database.record_day_off(user_id, today_str)

    # Sinkronisasi ke Google Sheets
    sheets_sync.trigger_sheet_sync(
        full_name=db_user.get("full_name", "Siswa"),
        school=db_user.get("school", "-"),
        nisn=db_user.get("nisn", "-"),
        att_type="Libur",
        office_name="-",
        date_str=today_str,
        time_str="-",
        distance=0,
        photo_path=""
    )

    day_name = "Sabtu" if now.weekday() == 5 else "Minggu"
    confirm_text = (
        f"🏖️ *Konfirmasi Libur Berhasil!*\n\n"
        f"📅 Tanggal: *{today_str} ({day_name})*\n"
        f"📌 Status: *Libur Akhir Pekan*\n\n"
        f"Status kehadiran Anda telah dicatat. Selamat beristirahat dan menikmati akhir pekan!"
    )
    await update.message.reply_text(confirm_text, reply_markup=get_main_keyboard(is_admin), parse_mode="Markdown")

# ==========================================================
# 5. HANDLER PENGAJUAN IZIN / SAKIT
# ==========================================================
async def leave_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Memulai alur pengajuan izin / sakit."""
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
    if att and att.get("status") in ("Sakit", "Izin", "Libur"):
        await update.message.reply_text(
            f"ℹ️ Anda sudah memiliki catatan status *{att['status']}* untuk hari ini ({today_str}).",
            reply_markup=get_main_keyboard(is_admin),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    leave_type_keyboard = ReplyKeyboardMarkup([
        [KeyboardButton("🤒 Sakit"), KeyboardButton("📝 Izin / Dispensasi")],
        [KeyboardButton("❌ Batal")]
    ], resize_keyboard=True)

    text = (
        "📄 **Pengajuan Izin / Sakit**\n\n"
        "Silakan pilih kategori ketidakhadiran Anda di bawah:"
    )
    await update.message.reply_text(text, reply_markup=leave_type_keyboard, parse_mode="Markdown")
    return LEAVE_TYPE

async def leave_type_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if "sakit" in text.lower():
        leave_type = "Sakit"
    elif "izin" in text.lower() or "dispensasi" in text.lower():
        leave_type = "Izin"
    else:
        await update.message.reply_text("Silakan pilih tombol '🤒 Sakit' atau '📝 Izin / Dispensasi':")
        return LEAVE_TYPE

    context.user_data["leave_type"] = leave_type
    prompt = (
        f"Kategori: *{leave_type}*\n\n"
        f"👉 Silakan ketik **alasan / keterangan** Anda (contoh: *Demam tinggi periksa ke dokter* atau *Ujian sekolah*):"
    )
    await update.message.reply_text(prompt, reply_markup=get_cancel_keyboard(), parse_mode="Markdown")
    return LEAVE_REASON

async def leave_reason_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reason = update.message.text.strip()
    if not reason:
        await update.message.reply_text("Alasan tidak boleh kosong. Silakan ketik alasan Anda:")
        return LEAVE_REASON

    context.user_data["leave_reason"] = reason
    prompt = (
        f"Alasan dicatat: *{reason}*\n\n"
        f"👉 Langkah terakhir: Silakan kirimkan **foto bukti surat** (surat dokter / surat dispensasi sekolah):"
    )
    await update.message.reply_text(prompt, reply_markup=get_cancel_keyboard(), parse_mode="Markdown")
    return LEAVE_PHOTO

async def leave_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photos = update.message.photo
    if not photos:
        await update.message.reply_text("Silakan kirimkan foto surat dokter atau surat dispensasi Anda:")
        return LEAVE_PHOTO

    user_id = update.effective_user.id
    is_admin = user_id in config.ADMIN_IDS
    today_str = utils.get_today_str()
    time_str = utils.get_current_time_str()

    photo = photos[-1]
    photo_file = await photo.get_file()

    filename = f"leave_{user_id}_{today_str}_{time_str.replace(':', '')}.jpg"
    filepath = config.PHOTOS_DIR / filename
    await photo_file.download_to_drive(custom_path=filepath)

    leave_type = context.user_data.get("leave_type", "Izin")
    reason = context.user_data.get("leave_reason", "-")

    req_id = database.create_leave_request(
        telegram_id=user_id,
        date_str=today_str,
        leave_type=leave_type,
        reason=reason,
        photo_path=str(filepath)
    )

    db_user = database.get_user(user_id)
    student_name = db_user.get("full_name", "Siswa") if db_user else "Siswa"
    school = db_user.get("school", "-") if db_user else "-"
    nisn = db_user.get("nisn", "-") if db_user else "-"

    user_confirm = (
        f"🎉 *Pengajuan {leave_type} Berhasil Dikirim!*\n\n"
        f"• Tanggal: *{today_str}*\n"
        f"• Alasan: *{reason}*\n\n"
        f"⏳ _Permintaan Anda telah diteruskan ke Admin untuk diverifikasi. Anda akan menerima notifikasi otomatis ketika disetujui._"
    )
    await update.message.reply_text(user_confirm, reply_markup=get_main_keyboard(is_admin), parse_mode="Markdown")

    admin_caption = (
        f"📋 *Permintaan Persetujuan {leave_type} Siswa!*\n\n"
        f"👤 **Nama:** {student_name}\n"
        f"🏫 **Sekolah:** {school}\n"
        f"🆔 **NISN:** `{nisn}`\n"
        f"📅 **Tanggal:** {today_str}\n"
        f"📝 **Alasan:** {reason}\n\n"
        f"Silakan periksa foto bukti di atas dan tentukan tindakan:"
    )
    admin_markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Setujui Izin", callback_data=f"leaveapp_{req_id}"),
            InlineKeyboardButton("❌ Tolak", callback_data=f"leaverej_{req_id}")
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
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.warning("Gagal kirim notif izin ke admin %s: %s", admin_id, e)

    context.user_data.clear()
    return ConversationHandler.END

async def leave_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menangani persetujuan atau penolakan pengajuan izin/sakit oleh Admin."""
    query = update.callback_query
    await query.answer()

    admin_id = update.effective_user.id
    if admin_id not in config.ADMIN_IDS:
        await query.answer("⛔ Anda tidak memiliki hak akses admin.", show_alert=True)
        return

    data = query.data
    action, req_id_str = data.split("_", 1)
    req_id = int(req_id_str)
    req = database.get_leave_request(req_id)

    if not req:
        await query.edit_message_caption(caption="❌ Data pengajuan izin tidak ditemukan.")
        return

    admin_name = update.effective_user.first_name or "Admin"
    student_id = req["telegram_id"]
    leave_type = req["leave_type"]
    date_str = req["date"]

    if action == "leaveapp":
        database.update_leave_request_status(req_id, "approved")
        database.record_leave_attendance(student_id, date_str, leave_type, req.get("photo_path", ""))

        # Sinkronisasi ke Google Sheets & Drive
        sheets_sync.trigger_sheet_sync(
            full_name=req.get("full_name", "Siswa"),
            school=req.get("school", "-"),
            nisn=req.get("nisn", "-"),
            att_type=leave_type,
            office_name="-",
            date_str=date_str,
            time_str="-",
            distance=0,
            photo_path=req.get("photo_path", "")
        )

        caption_edited = (
            f"✅ *Pengajuan {leave_type} Telah Disetujui!*\n\n"
            f"👤 Siswa: *{req.get('full_name', '-')}*\n"
            f"🏫 Sekolah: *{req.get('school', '-')}*\n"
            f"📅 Tanggal: *{date_str}*\n"
            f"📝 Alasan: *{req['reason']}*\n"
            f"🛡️ Disetujui oleh: *{admin_name}*"
        )
        await query.edit_message_caption(caption=caption_edited, parse_mode="Markdown")

        try:
            student_msg = (
                f"✅ *Pengajuan {leave_type} Anda Disetujui!*\n\n"
                f"📅 Tanggal: *{date_str}*\n"
                f"Status kehadiran Anda hari ini telah dicatat sebagai **{leave_type}**.\n"
                f"Semoga lekas sembuh / kegiatan Anda lancar!"
            )
            await context.bot.send_message(chat_id=student_id, text=student_msg, parse_mode="Markdown")
        except Exception as e:
            logger.warning("Gagal kirim notif persetujuan izin ke user %s: %s", student_id, e)

    elif action == "leaverej":
        database.update_leave_request_status(req_id, "rejected")
        caption_edited = (
            f"❌ *Pengajuan {leave_type} Ditolak!*\n\n"
            f"👤 Siswa: *{req.get('full_name', '-')}*\n"
            f"🏫 Sekolah: *{req.get('school', '-')}*\n"
            f"📅 Tanggal: *{date_str}*\n"
            f"📝 Alasan: *{req['reason']}*\n"
            f"🛡️ Ditolak oleh: *{admin_name}*"
        )
        await query.edit_message_caption(caption=caption_edited, parse_mode="Markdown")

        try:
            student_msg = (
                f"❌ *Pengajuan {leave_type} Ditolak!*\n\n"
                f"Pengajuan Anda untuk tanggal *{date_str}* ditolak oleh Admin.\n"
                f"Silakan hubungi pembimbing jika ada kekeliruan."
            )
            await context.bot.send_message(chat_id=student_id, text=student_msg, parse_mode="Markdown")
        except Exception as e:
            logger.warning("Gagal kirim notif penolakan izin ke user %s: %s", student_id, e)

# ==========================================================
# 6. HANDLER MENU UMUM (RIWAYAT, PROFIL, REKAP ADMIN)
# ==========================================================
async def profile_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_user = database.get_user(user_id)
    is_admin = user_id in config.ADMIN_IDS

    if not db_user:
        await update.message.reply_text("⚠️ Anda belum terdaftar. Silakan ketik /start.")
        return

    today_str = utils.get_today_str()
    today_att = database.get_today_attendance(user_id, today_str)

    in_office = f" ({today_att['check_in_office']})" if today_att and today_att.get("check_in_office") else ""
    out_office = f" ({today_att['check_out_office']})" if today_att and today_att.get("check_out_office") else ""

    in_status = f"{today_att['check_in_time']}{in_office}" if today_att and today_att.get("check_in_time") else "Belum"
    out_status = f"{today_att['check_out_time']}{out_office}" if today_att and today_att.get("check_out_time") else "Belum"
    act_status = today_att.get("activity") if today_att and today_att.get("activity") else "Belum diisi"

    office_list_text = "\n".join([f"• *{o['name']}* (Radius {o['radius']:,.0f}m)" for o in config.OFFICES])

    status_label = "Disetujui (Approved)" if db_user.get("status") == "approved" else ("Ditolak" if db_user.get("status") == "rejected" else "Menunggu Approval")
    now = utils.get_now()
    sched = utils.get_work_schedule(now)

    text = (
        f"👤 *Profil Pengguna*\n\n"
        f"• Nama: *{db_user['full_name']}*\n"
        f"• Asal Sekolah: *{db_user.get('school', '-')}*\n"
        f"• NISN: `{db_user.get('nisn', db_user.get('nik', '-'))}`\n"
        f"• ID Telegram: `{user_id}`\n"
        f"• Status Akun: *{status_label}*\n"
        f"• Role: *{db_user['role'].capitalize()}*\n\n"
        f"⏰ *Ketentuan Jam Absensi Hari Ini:*\n"
        f"• Periode Masuk: *{sched['rule']}*\n"
        f"• Batas Maksimal Masuk: *{sched['entry_time_str']} WIB*\n"
        f"• Jam Buka Absen Pulang: *{config.CHECKOUT_OPEN_HOUR:02d}:{config.CHECKOUT_OPEN_MINUTE:02d} WIB (5 Sore)*\n"
        f"• Batas Maksimal Cek Log Pulang: *23:59 WIB (12 Malam)*\n\n"
        f"📌 *Status Absensi Hari Ini ({today_str}):*\n"
        f"• Masuk: *{in_status}*\n"
        f"• Pulang: *{out_status}*\n"
        f"• Laporan Kegiatan: *{act_status}*\n\n"
        f"🏢 *Daftar Kantor Terdaftar:*\n"
        f"{office_list_text}"
    )
    await update.message.reply_text(text, reply_markup=get_main_keyboard(is_admin), parse_mode="Markdown")

async def history_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_admin = user_id in config.ADMIN_IDS
    records = database.get_user_history(user_id, limit=7)

    if not records:
        await update.message.reply_text(
            "Belum ada riwayat absensi yang tercatat.",
            reply_markup=get_main_keyboard(is_admin)
        )
        return

    lines = ["📋 *Riwayat 7 Absensi Terakhir:*", ""]
    for r in records:
        in_t = r.get("check_in_time") or "-"
        in_off = f" ({r['check_in_office']})" if r.get("check_in_office") else ""
        out_t = r.get("check_out_time") or "-"
        out_off = f" ({r['check_out_office']})" if r.get("check_out_office") else ""
        lines.append(f"📅 *{r['date']}*\n   Masuk: `{in_t}`{in_off}\n   Pulang: `{out_t}`{out_off}")

    await update.message.reply_text("\n".join(lines), reply_markup=get_main_keyboard(is_admin), parse_mode="Markdown")

async def rekap_admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    with open(csv_path, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename=Path(csv_path).name,
            caption=f"📊 *Laporan Rekap Absensi*\nTotal data: {len(records)} baris.",
            parse_mode="Markdown"
        )

async def admin_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menangani aksi persetujuan (approval) atau penolakan siswa oleh Admin."""
    query = update.callback_query
    await query.answer()

    admin_id = update.effective_user.id
    if admin_id not in config.ADMIN_IDS:
        await query.answer("⛔ Anda tidak memiliki hak akses admin.", show_alert=True)
        return

    data = query.data
    action, target_user_id_str = data.split("_", 1)
    target_user_id = int(target_user_id_str)
    target_user = database.get_user(target_user_id)

    if not target_user:
        await query.edit_message_text("❌ Data pengguna tidak ditemukan di database.")
        return

    admin_name = update.effective_user.first_name or "Admin"

    if action == "approve":
        database.update_user_status(target_user_id, "approved")
        text_edited = (
            f"✅ *Pendaftaran Disetujui!*\n\n"
            f"👤 **Siswa:** {target_user['full_name']}\n"
            f"🏫 **Sekolah:** {target_user.get('school', '-')}\n"
            f"🆔 **NISN:** `{target_user.get('nisn', '-')}`\n"
            f"🛡️ **Disetujui oleh:** {admin_name}"
        )
        await query.edit_message_text(text_edited, parse_mode="Markdown")

        # Kirim pemberitahuan ke siswa
        try:
            welcome_text = (
                f"🎉 *Selamat, {target_user['full_name']}!*\n\n"
                f"Pendaftaran absensi Anda telah **DISETUJUI** oleh Admin.\n"
                f"Sekarang Anda sudah dapat melakukan absensi masuk dan pulang.\n\n"
                f"Silakan gunakan menu di bawah untuk mulai absensi."
            )
            await context.bot.send_message(
                chat_id=target_user_id,
                text=welcome_text,
                reply_markup=get_main_keyboard(target_user_id in config.ADMIN_IDS),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning("Gagal kirim notif persetujuan ke user %s: %s", target_user_id, e)

    elif action == "reject":
        database.update_user_status(target_user_id, "rejected")
        text_edited = (
            f"❌ *Pendaftaran Ditolak!*\n\n"
            f"👤 **Siswa:** {target_user['full_name']}\n"
            f"🏫 **Sekolah:** {target_user.get('school', '-')}\n"
            f"🆔 **NISN:** `{target_user.get('nisn', '-')}`\n"
            f"🛡️ **Ditolak oleh:** {admin_name}"
        )
        await query.edit_message_text(text_edited, parse_mode="Markdown")

        # Kirim pemberitahuan ke siswa
        try:
            reject_text = (
                f"Halo, *{target_user['full_name']}*.\n\n"
                f"Pendaftaran absensi Anda **DITOLAK** oleh Admin.\n"
                f"Silakan hubungi pihak pembimbing/sekolah jika ada kekeliruan data."
            )
            await context.bot.send_message(
                chat_id=target_user_id,
                text=reject_text,
                reply_markup=ReplyKeyboardRemove(),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning("Gagal kirim notif penolakan ke user %s: %s", target_user_id, e)

async def send_reminder_to_students(bot, is_manual: bool = False) -> int:
    """Mengirim pesan broadcast pengingat absensi ke semua siswa yang belum absen masuk."""
    now = utils.get_now()
    today_str = utils.get_today_str()
    sched = utils.get_work_schedule(now)

    students = database.get_approved_students()
    sent_count = 0

    for student in students:
        student_id = student["telegram_id"]
        # Cek apakah siswa sudah absen, izin, sakit, atau libur hari ini
        att = database.get_today_attendance(student_id, today_str)
        is_already_handled = att and (
            att.get("status") in ("Hadir", "Sakit", "Izin", "Libur") or 
            (att.get("check_in_time") and att.get("check_in_time") != "-")
        )
        if not is_already_handled:
            reminder_text = (
                f"⏰ *PENGINGAT ABSENSI MASUK*\n\n"
                f"Selamat pagi, *{student['full_name']}*!\n\n"
                f"Hari ini batas maksimal absensi masuk adalah pukul *{sched['entry_time_str']} WIB* (1 jam lagi).\n\n"
                f"📌 *Ketentuan Jam Masuk Hari Ini:*\n"
                f"• Periode: *{sched['rule']}*\n"
                f"• Batas Akhir Masuk: *{sched['entry_time_str']} WIB*\n"
                f"• Lokasi: Kantor Blimbing atau Kantor Juanda\n\n"
                f"⚠️ *PERINGATAN:* Jika melewati pukul *{sched['entry_time_str']} WIB*, absensi masuk otomatis dikunci/ditolak sistem dan Anda harus melapor ke pembimbing.\n\n"
                f"Segera lakukan absensi masuk jika Anda sudah tiba di lokasi kantor!"
            )
            try:
                await bot.send_message(
                    chat_id=student_id,
                    text=reminder_text,
                    reply_markup=get_main_keyboard(False),
                    parse_mode="Markdown"
                )
                sent_count += 1
            except Exception as e:
                logger.warning("Gagal mengirim pengingat ke siswa %s: %s", student_id, e)

    return sent_count

async def send_checkout_reminder_to_students(bot) -> int:
    """Mengirim pesan broadcast pengingat absen pulang ke siswa yang sudah masuk tapi belum pulang."""
    today_str = utils.get_today_str()
    students = database.get_approved_students()
    sent_count = 0

    for student in students:
        student_id = student["telegram_id"]
        att = database.get_today_attendance(student_id, today_str)

        has_checked_in = att and att.get("check_in_time") and att.get("check_in_time") != "-"
        has_checked_out = att and att.get("check_out_time") and att.get("check_out_time") != "-"
        is_day_off = att and att.get("status") in ("Libur", "Izin", "Sakit")

        if has_checked_in and not has_checked_out and not is_day_off:
            reminder_text = (
                f"⏰ *PENGINGAT CEK LOG PULANG & LAPORAN KEGIATAN*\n\n"
                f"Halo, *{student['full_name']}*!\n\n"
                f"Anda tercatat sudah melakukan *Absen Masuk* hari ini, namun **belum melakukan Absen Pulang**.\n\n"
                f"⚠️ *Batas Waktu Cek Log:* Batas maksimal untuk absen pulang dan mengirimkan laporan kegiatan adalah pukul **23:59 WIB (12 malam)**.\n\n"
                f"Silakan tekan tombol **'🔴 Absen Pulang'** sekarang agar kehadiran dan laporan kegiatan Anda tercatat lengkap!"
            )
            try:
                await bot.send_message(
                    chat_id=student_id,
                    text=reminder_text,
                    reply_markup=get_main_keyboard(False),
                    parse_mode="Markdown"
                )
                sent_count += 1
            except Exception as e:
                logger.warning("Gagal mengirim pengingat pulang ke siswa %s: %s", student_id, e)

    return sent_count

async def reminder_scheduler_loop(bot):
    """
    Background loop yang berjalan setiap 30 detik untuk mengecek waktu pengingat:
    - Tanggal 1 s/d 12: Pengingat masuk otomatis dikirim pukul 07:00 WIB (1 jam sebelum masuk 08:00 WIB)
    - Tanggal 13 s/d akhir: Pengingat masuk otomatis dikirim pukul 08:00 WIB (1 jam sebelum masuk 09:00 WIB)
    - Setiap Malam (20:00 WIB): Pengingat cek log pulang bagi yang belum absen pulang (batas max 12 malam)
    """
    last_morning_broadcast_date = ""
    last_evening_broadcast_date = ""
    logger.info("Scheduler pengingat absensi otomatis aktif.")

    while True:
        try:
            now = utils.get_now()
            today_str = utils.get_today_str()
            sched = utils.get_work_schedule(now)

            # 1. Cek pengingat jam masuk pagi
            if (now.hour == sched["reminder_hour"] and 
                now.minute == sched["reminder_minute"] and 
                last_morning_broadcast_date != today_str):

                last_morning_broadcast_date = today_str
                logger.info("Mengirim broadcast pengingat masuk otomatis (%s)...", sched["rule"])
                total_sent = await send_reminder_to_students(bot)
                logger.info("Pengingat masuk otomatis berhasil dikirim ke %d siswa.", total_sent)

            # 2. Cek pengingat cek log pulang malam (20:00 WIB)
            if (now.hour == 20 and 
                now.minute == 0 and 
                last_evening_broadcast_date != today_str):

                last_evening_broadcast_date = today_str
                logger.info("Mengirim broadcast pengingat cek log pulang (20:00 WIB)...")
                total_checkout_sent = await send_checkout_reminder_to_students(bot)
                logger.info("Pengingat pulang malam berhasil dikirim ke %d siswa.", total_checkout_sent)

        except Exception as e:
            logger.error("Error pada scheduler pengingat: %s", e)

        await asyncio.sleep(30)

async def broadcast_pengingat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Perintah manual bagi Admin untuk mengirimkan broadcast pengingat sekarang (/kirimpengingat)."""
    user_id = update.effective_user.id
    if user_id not in config.ADMIN_IDS:
        await update.message.reply_text("⛔ Perintah ini khusus untuk Admin.")
        return

    await update.message.reply_text("⏳ Sedang mengirimkan pengingat ke seluruh siswa yang belum absen masuk hari ini...")
    total_sent = await send_reminder_to_students(context.bot, is_manual=True)
    await update.message.reply_text(
        f"✅ Selesai! Pengingat berhasil dikirim ke *{total_sent} siswa* yang belum absen masuk hari ini.",
        parse_mode="Markdown"
    )

async def broadcast_pengingat_pulang_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Perintah manual bagi Admin untuk mengirimkan broadcast pengingat pulang sekarang (/kirimpengingatpulang)."""
    user_id = update.effective_user.id
    if user_id not in config.ADMIN_IDS:
        await update.message.reply_text("⛔ Perintah ini khusus untuk Admin.")
        return

    await update.message.reply_text("⏳ Sedang mengirimkan pengingat pulang ke siswa yang belum cek log pulang...")
    total_sent = await send_checkout_reminder_to_students(context.bot)
    await update.message.reply_text(
        f"✅ Selesai! Pengingat pulang berhasil dikirim ke *{total_sent} siswa* yang belum checkout hari ini.",
        parse_mode="Markdown"
    )

async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Membatalkan alur percakapan."""
    user_id = update.effective_user.id
    is_admin = user_id in config.ADMIN_IDS
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Proses telah dibatalkan.",
        reply_markup=get_main_keyboard(is_admin)
    )
    return ConversationHandler.END

# ==========================================================
# 5. MAIN ENTRY POINT
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

    # Inisialisasi database
    database.init_db()
    logger.info("Database berhasil diinisialisasi.")
    logger.info("Daftar Kantor Terdaftar (%d kantor):", len(config.OFFICES))
    for o in config.OFFICES:
        logger.info(" - %s: (%s, %s) radius %sm", o['name'], o['lat'], o['lon'], o['radius'])

    # Inisialisasi Bot Application
    async def on_startup(application: Application):
        # Aktifkan scheduler pengingat otomatis di background
        asyncio.create_task(reminder_scheduler_loop(application.bot))

    app = ApplicationBuilder().token(config.BOT_TOKEN).post_init(on_startup).build()

    # Handler pembatalan
    cancel_handlers = [
        CommandHandler("cancel", cancel_handler),
        CommandHandler("batal", cancel_handler),
        MessageHandler(filters.Regex("^❌ Batal$"), cancel_handler),
    ]

    # 1. Conversation Handler Registrasi
    reg_conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_command),
            CommandHandler("daftar", start_command)
        ],
        states={
            REG_NAME: [
                *cancel_handlers,
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_name_handler)
            ],
            REG_SCHOOL: [
                *cancel_handlers,
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_school_handler)
            ],
            REG_NISN: [
                *cancel_handlers,
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_nisn_handler)
            ]
        },
        fallbacks=cancel_handlers
    )

    # 2. Conversation Handler Absen Masuk
    checkin_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^🟢 Absen Masuk$"), checkin_start),
            CommandHandler("masuk", checkin_start)
        ],
        states={
            CHECKIN_LOC: [
                *cancel_handlers,
                MessageHandler(filters.LOCATION, checkin_location_received)
            ],
            CHECKIN_PHOTO: [
                *cancel_handlers,
                MessageHandler(filters.PHOTO, checkin_photo_received)
            ]
        },
        fallbacks=cancel_handlers
    )

    # 3. Conversation Handler Absen Pulang
    checkout_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^🔴 Absen Pulang$"), checkout_start),
            CommandHandler("pulang", checkout_start)
        ],
        states={
            CHECKOUT_LOC: [
                *cancel_handlers,
                MessageHandler(filters.LOCATION, checkout_location_received)
            ],
            CHECKOUT_PHOTO: [
                *cancel_handlers,
                MessageHandler(filters.PHOTO, checkout_photo_received)
            ],
            CHECKOUT_ACTIVITY: [
                *cancel_handlers,
                MessageHandler(filters.TEXT & ~filters.COMMAND, checkout_activity_received)
            ]
        },
        fallbacks=cancel_handlers
    )

    # 4. Conversation Handler Pengajuan Izin / Sakit
    leave_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^📄 Ajukan Izin / Sakit$"), leave_start),
            CommandHandler("izin", leave_start)
        ],
        states={
            LEAVE_TYPE: [
                *cancel_handlers,
                MessageHandler(filters.TEXT & ~filters.COMMAND, leave_type_received)
            ],
            LEAVE_REASON: [
                *cancel_handlers,
                MessageHandler(filters.TEXT & ~filters.COMMAND, leave_reason_received)
            ],
            LEAVE_PHOTO: [
                *cancel_handlers,
                MessageHandler(filters.PHOTO, leave_photo_received)
            ]
        },
        fallbacks=cancel_handlers
    )

    # 5. Conversation Handler Laporan Kegiatan Harian
    activity_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^📝 Laporan Kegiatan$"), activity_report_start),
            CommandHandler("lapor", activity_report_start)
        ],
        states={
            ACTIVITY_TEXT: [
                *cancel_handlers,
                MessageHandler(filters.TEXT & ~filters.COMMAND, activity_report_received)
            ]
        },
        fallbacks=cancel_handlers
    )

    # Daftarkan handlers
    app.add_handler(reg_conv)
    app.add_handler(checkin_conv)
    app.add_handler(checkout_conv)
    app.add_handler(leave_conv)
    app.add_handler(activity_conv)
    app.add_handler(CallbackQueryHandler(admin_approval_callback, pattern="^(approve|reject)_"))
    app.add_handler(CallbackQueryHandler(leave_approval_callback, pattern="^(leaveapp|leaverej)_"))

    # Handler Libur Akhir Pekan (Sabtu & Minggu)
    app.add_handler(MessageHandler(filters.Regex("^🏖️ Libur Hari Ini$"), day_off_handler))
    app.add_handler(CommandHandler("libur", day_off_handler))

    # Menu statis
    app.add_handler(MessageHandler(filters.Regex("^📝 Laporan Kegiatan$"), activity_report_start))
    app.add_handler(MessageHandler(filters.Regex("^👤 Profil Saya$"), profile_handler))
    app.add_handler(MessageHandler(filters.Regex("^📋 Riwayat Absensi$"), history_handler))
    app.add_handler(MessageHandler(filters.Regex("^📊 Rekap Absensi \\(Admin\\)$"), rekap_admin_handler))
    app.add_handler(CommandHandler("rekap", rekap_admin_handler))
    app.add_handler(CommandHandler("kirimpengingat", broadcast_pengingat_command))
    app.add_handler(CommandHandler("kirimpengingatpulang", broadcast_pengingat_pulang_command))
    app.add_handler(CommandHandler("lapor", activity_report_start))
    app.add_handler(CommandHandler("profil", profile_handler))
    app.add_handler(CommandHandler("riwayat", history_handler))
    app.add_handler(CommandHandler("cancel", cancel_handler))

    logger.info("Bot absensi Telegram siap dijalankan!")
    app.run_polling()

if __name__ == "__main__":
    main()
