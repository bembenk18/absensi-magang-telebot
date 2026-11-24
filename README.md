# 🤖 Sistem Absensi Karyawan via Telegram Bot

Sistem absensi berbasis Telegram Bot (khusus chat privat) dengan verifikasi **Lokasi GPS (Geofencing)** dan **Foto Selfie (Bukti Kehadiran)**, database lokal SQLite, rekap harian otomatis setelah jam pulang, sinkronisasi Google Sheets/Drive terautentikasi HMAC-SHA256, serta ekspor rekap absensi CSV untuk Admin/HRD.

---

## 🌟 Fitur Utama

- **Pendaftaran Siswa Baru & Approval Admin:** Siswa memasukkan **Nama Lengkap**, **Asal Sekolah**, dan **NISN** (5–20 digit angka). Admin menerima notifikasi instan dengan tombol `[✅ Setujui]` atau `[❌ Tolak]`.
- **Tombol Libur Akhir Pekan (Sabtu & Minggu):** Pada hari Sabtu dan Minggu, tombol **`[🏖️ Libur Hari Ini]`** otomatis muncul di menu utama bagi siswa yang memiliki jadwal libur. Status langsung tercatat dan tersinkron ke Google Sheet.
- **Pengajuan Izin & Sakit (`📄 Ajukan Izin / Sakit`):** Siswa dapat mengajukan izin/sakit dengan memilih kategori, mengisi alasan, dan mengunggah foto surat dokter/surat sekolah. Admin menerima notifikasi instan untuk menyetujui atau menolak.
- **Laporan Kegiatan Harian (`📝 Laporan Kegiatan`):** Siswa melaporkan apa saja kegiatan yang dikerjakan selama seharian saat Absen Pulang atau via menu laporan mandiri. Laporan tersimpan di database dan langsung tersinkron ke kolom Google Sheets & rekap CSV.
- **Aturan Jam Masuk Dinamis & Kunci Otomatis (Cutoff):**
  - **Tanggal 1 - 12:** Jam masuk **08:00 WIB** (Broadcast pengingat jam **07:00 WIB**).
  - **Tanggal 13 - akhir bulan:** Jam masuk **09:00 WIB** (Broadcast pengingat jam **08:00 WIB**).
  - Jika siswa absen melewati batas jam masuk tersebut, sistem **otomatis menolak** dan menginstruksikan siswa untuk segera menghubungi pembimbing.
- **Jam Buka Cek Log Pulang (Mulai 17:00 WIB s/d 23:59 WIB):** Absen pulang baru dibuka mulai pukul **17:00 WIB (5 sore)**. Jika siswa mencoba absen pulang sebelum jam 17:00 WIB, bot otomatis menolak. Batas maksimal cek log adalah pukul **23:59 WIB (12 malam)**.
- **Broadcast Pengingat Malam (20:00 WIB):** Bot mengirim pesan broadcast pengingat otomatis pukul 20:00 WIB khusus ke siswa yang sudah masuk namun belum pulang.
- **Rekap Absensi Harian Otomatis & Perintah `/rekapharian`:**
  - Setelah jam pulang (default `18:00` sesuai `TIMEZONE`, dapat diubah lewat `RECAP_HOUR` & `RECAP_MINUTE`), bot otomatis mengirim ringkasan kehadiran seluruh siswa `approved` (Hadir Lengkap, Hadir Belum Pulang, Izin, Sakit, Libur, dan Belum Absen) ke `RECAP_RECIPIENT_IDS` (fallback ke `ADMIN_IDS`).
  - Pengiriman otomatis tercatat di tabel `scheduled_job_logs` SQLite agar tidak mengirim duplikat pada tanggal yang sama meskipun bot restart.
  - Admin dapat meminta rekap hari ini kapan saja melalui perintah `/rekapharian` (dikirim langsung ke admin peminta tanpa mengganggu penjadwalan otomatis).
- **Integrasi Real-time Google Sheets & Google Drive (HMAC-SHA256):** Setiap absensi, laporan kegiatan, izin/sakit, dan libur tercatat di Google Sheet dengan proteksi formula injection, dan foto (selfie / surat dokter) terunggah privat ke Google Drive.
- **Validasi Multi-Kantor (Geofencing GPS):** Mendukung beberapa kantor dengan radius > 0 meter serta penolakan lokasi forward/bot.
- **Profil & Riwayat:** Melihat status persetujuan, ketentuan jam masuk hari ini, dan riwayat absensi.
- **Laporan Admin (Rekap CSV):** Admin dapat mengunduh rekapan data absensi lengkap dalam format `.csv` (dengan proteksi formula injection dan pembersihan file sementara).

---

## 📁 Struktur Direktori

```text
absensi-tele/
├── config.py             # Konfigurasi aplikasi & membaca file .env
├── database.py           # Skema SQLite, WAL/foreign keys, & fungsi CRUD
├── utils.py              # Rumus Haversine (jarak GPS), waktu WIB, & generator CSV aman
├── recap.py              # Modul rekap absensi harian (klasifikasi, format HTML, pengiriman)
├── sheets_sync.py        # Klien async HTTPS + HMAC-SHA256 untuk Google Apps Script
├── google_apps_script.js # Endpoint Google Apps Script (doPost) dengan verifikasi HMAC
├── main.py               # Entry point bot Telegram, handler chat privat, & scheduler
├── requirements.txt      # Daftar dependensi Python
├── .env.example          # Contoh template konfigurasi environment
├── photos/               # Tempat penyimpanan foto selfie absensi
└── reports/              # Direktori sementara pembuatan file CSV
```

---

## 🚀 Panduan Memulai (Step-by-step)

### 1. Buat Bot di Telegram (@BotFather)
1. Buka aplikasi Telegram, cari bot **[@BotFather](https://t.me/BotFather)**.
2. Kirim pesan `/newbot` dan ikuti instruksi hingga mendapatkan **API Token**.

### 2. Dapatkan ID Telegram Anda (Untuk Hak Admin)
1. Buka Telegram, cari bot **[@userinfobot](https://t.me/userinfobot)**.
2. Klik **Start** untuk melihat `Id` numerik Anda.

### 3. Konfigurasi File `.env`
Salin template konfigurasi dan perketat izin file:
```bash
cp .env.example .env
chmod 600 .env
```
Sesuaikan isi `.env` (token, koordinat kantor, `ADMIN_IDS`, pengaturan `RECAP_*`, serta `GOOGLE_SHEET_WEBHOOK_URL` dan `GOOGLE_SHEET_WEBHOOK_SECRET`).

Variabel rekap harian yang tersedia:
- `RECAP_ENABLED=true` — aktif/nonaktifkan rekap harian otomatis.
- `RECAP_HOUR=18` & `RECAP_MINUTE=0` — jadwal pengiriman harian (setelah jam pulang).
- `RECAP_SKIP_WEEKENDS=false` — atur `true` jika ingin melewati Sabtu & Minggu.
- `RECAP_RECIPIENT_IDS=` — ID Telegram penerima (pisahkan koma); jika kosong akan otomatis memakai `ADMIN_IDS`.
- `RECAP_SEND_CSV=false` — atur `true` untuk menyertakan lampiran CSV harian.

---

### 4. Jalankan Bot
Aktifkan virtual environment dan jalankan `main.py`:
```bash
source .venv/bin/activate
python3 main.py
```

---

## 📊 Integrasi Google Sheets & Google Drive (Terautentikasi HMAC)

1. Buat spreadsheet baru di [Google Sheets](https://sheets.new).
2. Klik menu **Extensions (Ekstensi)** > **Apps Script**.
3. Hapus kode bawaan, lalu salin dan tempel seluruh isi file [`google_apps_script.js`](google_apps_script.js).
4. Buka **Project Settings** (ikon gerigi) > **Script Properties** > **Add script property**:
   - Property: `WEBHOOK_SECRET`
   - Value: secret acak minimal 32 karakter (harus sama persis dengan `GOOGLE_SHEET_WEBHOOK_SECRET` di `.env`).
5. Klik **Save**, lalu **Deploy** > **New deployment** > pilih **Web app**:
   - Description: `Webhook Absensi`
   - Execute as: `Me`
   - Who has access: `Anyone` *(Endpoint tetap memverifikasi tanda tangan HMAC-SHA256, timestamp, dan nonce sebelum memproses data)*.
6. Salin **Web app URL** (`https://script.google.com/macros/s/.../exec`) ke `GOOGLE_SHEET_WEBHOOK_URL` di `.env`, dan isi `GOOGLE_SHEET_WEBHOOK_SECRET` dengan secret yang sama.

---

## 🐳 Menjalankan dengan Docker (Rekomendasi Production)

```bash
docker compose up -d --build
docker compose logs -f
docker compose down
```

---

## 🛡️ Keamanan & Operasional
- Simpan `.env` dan `attendance.db` dengan izin `chmod 600`, serta direktori `photos/` dan `reports/` dengan izin `chmod 700`.
- Seluruh interaksi bot dibatasi ke chat privat (`ChatType.PRIVATE`) untuk mencegah kebocoran data siswa di grup.
- Untuk deployment VPS non-Docker, gunakan `systemd` service dengan `User` non-root dan izin file log yang terbatas.
