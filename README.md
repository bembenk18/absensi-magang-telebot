# 🤖 Sistem Absensi Karyawan via Telegram Bot

Sistem absensi berbasis Telegram Bot dengan verifikasi **Lokasi GPS (Geofencing)** dan **Foto Selfie (Bukti Kehadiran)**, database lokal SQLite, serta ekspor rekap absensi ke CSV untuk Admin/HRD.

---

## 🌟 Fitur Utama

- **Pendaftaran Siswa Baru & Approval Admin:** Siswa memasukkan **Nama Lengkap**, **Asal Sekolah**, dan **NISN**. Admin menerima notifikasi instan dengan tombol `[✅ Setujui]` atau `[❌ Tolak]`.
- **Tombol Libur Akhir Pekan (Sabtu & Minggu):** Pada hari Sabtu dan Minggu, tombol **`[🏖️ Libur Hari Ini]`** otomatis muncul di menu utama bagi siswa yang memiliki jadwal libur. Status langsung tercatat dan tersinkron ke Google Sheet.
- **Pengajuan Izin & Sakit (`📄 Ajukan Izin / Sakit`):** Siswa dapat mengajukan izin/sakit dengan memilih kategori, mengisi alasan, dan mengunggah foto surat dokter/surat sekolah. Admin menerima notifikasi instan untuk menyetujui atau menolak.
- **Aturan Jam Masuk Dinamis & Kunci Otomatis (Cutoff):**
  - **Tanggal 1 - 12:** Jam masuk **08:00 WIB** (Broadcast pengingat jam **07:00 WIB**).
  - **Tanggal 13 - akhir bulan:** Jam masuk **09:00 WIB** (Broadcast pengingat jam **08:00 WIB**).
  - Jika siswa absen melewati batas jam masuk tersebut, sistem **otomatis menolak** dan menginstruksikan siswa untuk segera menghubungi pembimbing.
- **Broadcast Pengingat Cerdas (1 Jam Sebelum Masuk):** Otomatis dikirim ke siswa yang belum absen. Siswa yang sudah absen, libur, atau izin/sakit otomatis tidak akan diganggu pesan pengingat.
- **Integrasi Real-time Google Sheets & Google Drive:** Setiap absensi, izin/sakit, dan libur otomatis tercatat di Google Sheet, dan foto (selfie / surat dokter) langsung terunggah ke Google Drive dengan link yang bisa diklik.
- **Validasi Multi-Kantor (Geofencing GPS):** Mendukung Kantor Blimbing & Kantor Juanda (radius 100 meter).
- **Profil & Riwayat:** Melihat status persetujuan, ketentuan jam masuk hari ini, dan riwayat absensi.
- **Laporan Admin (Rekap CSV):** Admin dapat mengunduh rekapan data absensi lengkap dengan data sekolah dan NISN dalam format `.csv` untuk Excel/Spreadsheet.

---

## 📁 Struktur Direktori

```text
absensi-tele/
├── config.py           # Konfigurasi aplikasi & membaca file .env
├── database.py         # Skema SQLite & fungsi CRUD absensi & user
├── utils.py            # Rumus Haversine (jarak GPS), waktu WIB, & generator CSV
├── main.py             # Entry point bot Telegram & percakapan interaktif
├── requirements.txt    # Daftar dependensi Python
├── .env.example        # Contoh template konfigurasi environment
├── photos/             # Tempat penyimpanan foto selfie absensi
└── reports/            # Tempat penyimpanan file laporan rekap CSV
```

---

## 🚀 Panduan Memulai (Step-by-step)

### 1. Buat Bot di Telegram (@BotFather)
1. Buka aplikasi Telegram, cari bot **[@BotFather](https://t.me/BotFather)**.
2. Kirim pesan `/newbot`.
3. Masukkan nama bot (contoh: `Absensi Kantor Bot`).
4. Masukkan username bot berakhiran `bot` (contoh: `absensi_kantor_xyz_bot`).
5. Salin **API Token** yang diberikan (formatnya seperti `712345678:AAH...`).

### 2. Dapatkan ID Telegram Anda (Untuk Hak Admin)
1. Buka Telegram, cari bot **[@userinfobot](https://t.me/userinfobot)**.
2. Klik **Start**, bot akan membalas dengan `Id` angka Anda (contoh: `123456789`).

### 3. Tentukan Titik Koordinat Kantor
1. Buka Google Maps melalui browser atau HP.
2. Klik kanan (atau tekan tahan di HP) pada titik lokasi kantor Anda.
3. Salin angka koordinatnya, misalnya: `-6.175392, 106.827153`.

---

### 4. Konfigurasi File `.env`
Salin template konfigurasi:
```bash
cp .env.example .env
```
Lalu edit file `.env`. Untuk mendukung **2 kantor (atau lebih)**, gunakan format `OFFICE_1_...` dan `OFFICE_2_...`:
```ini
# Masukkan API Token dari BotFather
BOT_TOKEN=712345678:AAHxxxxxxxxxxxxxxxxxxxxxxxxx

# Kantor 1 (Contoh: Kantor Pusat)
OFFICE_1_NAME=Kantor Pusat
OFFICE_1_LAT=-6.175392
OFFICE_1_LON=106.827153
OFFICE_1_RADIUS=100

# Kantor 2 (Contoh: Kantor Cabang)
OFFICE_2_NAME=Kantor Cabang
OFFICE_2_LAT=-6.917464
OFFICE_2_LON=107.619123
OFFICE_2_RADIUS=100

# Zona waktu (WIB: Asia/Jakarta, WITA: Asia/Makassar, WIT: Asia/Jayapura)
TIMEZONE=Asia/Jakarta

# Masukkan ID Telegram Admin (bisa lebih dari satu, pisahkan dengan koma)
ADMIN_IDS=123456789
```
> *Catatan:* Karyawan cukup berada di dalam radius **salah satu kantor** untuk bisa melakukan absen masuk/pulang. Sistem akan otomatis mendeteksi kantor mana yang dituju dan menyimpannya di laporan.

---

### 5. Jalankan Bot
Aktifkan virtual environment dan jalankan `main.py`:
```bash
source .venv/bin/activate
python3 main.py
```

Jika berhasil, terminal akan menampilkan pesan:
```text
INFO - Database berhasil diinisialisasi.
INFO - Bot absensi Telegram siap dijalankan!
```

---

## 📊 Integrasi Google Sheets & Google Drive (Otomatis)

Setiap ada absensi masuk atau pulang, data dan foto selfie otomatis tercatat di Google Sheet dan tersimpan di Google Drive:

1. Buat spreadsheet baru di [Google Sheets](https://sheets.new).
2. Klik menu **Extensions (Ekstensi)** > **Apps Script**.
3. Hapus kode bawaan, lalu salin dan tempel seluruh isi file [google_apps_script.js](file:///home/bayu/absensi-tele/google_apps_script.js).
4. Klik tombol **Save** (💾).
5. Klik tombol biru **Deploy** (Terapkan) > **New deployment**.
6. Pilih jenis **Web app**:
   - Description: `Webhook Absensi`
   - Execute as: `Me`
   - Who has access: `Anyone` (Siapa saja)  *(Penting agar bot bisa kirim data!)*
7. Klik **Deploy** dan izinkan akses Google Anda.
8. Salin **Web app URL** yang diberikan (format: `https://script.google.com/macros/s/.../exec`).
9. Buka file [.env](file:///home/bayu/absensi-tele/.env) dan masukkan URL tersebut:
   ```ini
   GOOGLE_SHEET_WEBHOOK_URL=https://script.google.com/macros/s/AKfycbx.../exec
   ```
10. Selesai! Saat siswa absen, data langsung masuk ke Google Sheet lengkap dengan link foto Google Drive.

---

## 🐳 Menjalankan dengan Docker (Rekomendasi Production)

Sistem sudah dilengkapi dengan `Dockerfile` dan `docker-compose.yml`. Semua data penting (`attendance.db`, `photos/`, `reports/`) disimpan menggunakan volume persisten sehingga tidak akan hilang saat kontainer diperbarui.

### 1. Jalankan Kontainer di Background
```bash
docker compose up -d --build
```

### 2. Melihat Log Bot Berjalan
```bash
docker compose logs -f
```

### 3. Menghentikan Kontainer
```bash
docker compose down
```

---

## 📱 Cara Penggunaan oleh Siswa

1. Buka bot di Telegram, klik **Start** (atau kirim `/start`).
2. Masukkan **Nama Lengkap** dan **NIK**.
3. Tombol menu interaktif akan muncul:
   * **🟢 Absen Masuk:** Tekan tombol, kirim lokasi GPS, lalu kirim foto selfie.
   * **🔴 Absen Pulang:** Tekan tombol, kirim lokasi GPS, lalu kirim foto selfie.
   * **📋 Riwayat Absensi:** Melihat catatan kehadiran.
   * **👤 Profil Saya:** Melihat status kehadiran hari ini.
   * **📊 Rekap Absensi (Admin):** Khusus ID yang didaftarkan di `ADMIN_IDS` untuk download file CSV rekap.

---

## 🛡️ Tips Tambahan & Rekomendasi Deployment
- Untuk menjalankan bot terus-menerus di server VPS (24/7), Anda dapat menggunakan **`systemd`** service atau **`tmux` / `screen`**:
  ```bash
  # Contoh menggunakan nohup di background:
  nohup .venv/bin/python3 main.py > bot.log 2>&1 &
  ```
