/**
 * ============================================================================
 * GOOGLE APPS SCRIPT - SINKRONISASI ABSENSI TELEGRAM
 * ============================================================================
 * 
 * CARA MEMASANG DI GOOGLE SPREADSHEET:
 * 1. Buka spreadsheet baru di Google Sheets (sheets.new).
 * 2. Klik menu: Extensions (Ekstensi) > Apps Script.
 * 3. Hapus kode bawaan (myFunction), lalu paste seluruh kode di bawah ini.
 * 4. Klik tombol "Save" (ikon disket 💾).
 * 5. Klik tombol biru "Deploy" (Terapkan) di pojok kanan atas > "New deployment" (Penerapan baru).
 * 6. Klik ikon gerigi ⚙️ di sebelah "Select type" > pilih "Web app".
 * 7. Isi form:
 *    - Description: Webhook Absensi
 *    - Execute as: Me (email akun Google Anda)
 *    - Who has access: Anyone (Siapa saja)  <-- PENTING! Agar bot bisa kirim data.
 * 8. Klik "Deploy". Jika muncul "Authorize access", izinkan dengan akun Google Anda.
 * 9. Salin "Web app URL" (format: https://script.google.com/macros/s/.../exec).
 * 10. Masukkan URL tersebut ke file .env bot:
 *     GOOGLE_SHEET_WEBHOOK_URL=https://script.google.com/macros/s/.../exec
 */

function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      return ContentService.createTextOutput(JSON.stringify({
        status: "error",
        message: "Tidak ada data post yang diterima"
      })).setMimeType(ContentService.MimeType.JSON);
    }

    var data = JSON.parse(e.postData.contents);

    var date = data.date || Utilities.formatDate(new Date(), "Asia/Jakarta", "yyyy-MM-dd");
    var time = data.time || Utilities.formatDate(new Date(), "Asia/Jakarta", "HH:mm:ss");
    var fullName = data.full_name || "-";
    var school = data.school || "-";
    var nisn = data.nisn || "-";
    var type = data.type || "Masuk";
    var officeName = data.office_name || "Kantor";
    var distance = data.distance !== undefined ? Math.round(data.distance) : 0;
    var imageB64 = data.image_base64 || "";
    var filename = data.filename || ("selfie_" + date + "_" + time.replace(/:/g, "") + ".jpg");

    var fileUrl = "-";

    // 1. Simpan Foto Selfie ke Google Drive
    if (imageB64) {
      var folderName = "Foto Absensi Siswa";
      var folders = DriveApp.getFoldersByName(folderName);
      var folder;
      if (folders.hasNext()) {
        folder = folders.next();
      } else {
        folder = DriveApp.createFolder(folderName);
      }

      var decoded = Utilities.base64Decode(imageB64);
      var blob = Utilities.newBlob(decoded, "image/jpeg", filename);
      var file = folder.createFile(blob);

      // Beri izin link agar foto bisa dibuka oleh pembimbing/admin
      file.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
      fileUrl = file.getUrl();
    }

    // 2. Tambahkan Data ke Google Sheet Aktif
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = ss.getActiveSheet();

    // Buat Header jika sheet masih kosong
    if (sheet.getLastRow() === 0) {
      sheet.appendRow([
        "Tanggal",
        "Waktu (WIB)",
        "Nama Siswa",
        "Asal Sekolah",
        "NISN",
        "Tipe",
        "Lokasi Kantor",
        "Jarak (m)",
        "Link Foto Drive"
      ]);

      // Styling Header (Warna Biru, Teks Putih, Tebal)
      var headerRange = sheet.getRange(1, 1, 1, 9);
      headerRange.setFontWeight("bold");
      headerRange.setBackground("#2563eb");
      headerRange.setFontColor("#ffffff");
      headerRange.setHorizontalAlignment("center");
      sheet.setFrozenRows(1);
    }

    // Format Link Foto
    var photoCell = fileUrl !== "-" ? '=HYPERLINK("' + fileUrl + '", "📸 Lihat Foto")' : "-";

    // Tambah baris baru
    sheet.appendRow([
      date,
      time,
      fullName,
      school,
      nisn,
      type,
      officeName,
      distance,
      photoCell
    ]);

    // Rata tengah beberapa kolom agar rapi
    var lastRow = sheet.getLastRow();
    sheet.getRange(lastRow, 1).setHorizontalAlignment("center"); // Tanggal
    sheet.getRange(lastRow, 2).setHorizontalAlignment("center"); // Waktu
    sheet.getRange(lastRow, 5).setNumberFormat("@").setHorizontalAlignment("center"); // NISN sebagai teks
    sheet.getRange(lastRow, 6).setHorizontalAlignment("center"); // Tipe
    sheet.getRange(lastRow, 8).setHorizontalAlignment("right");  // Jarak
    sheet.getRange(lastRow, 9).setHorizontalAlignment("center"); // Link Foto

    return ContentService.createTextOutput(JSON.stringify({
      status: "success",
      message: "Data absensi dan foto berhasil disimpan ke Google Sheets & Drive",
      file_url: fileUrl
    })).setMimeType(ContentService.MimeType.JSON);

  } catch (error) {
    return ContentService.createTextOutput(JSON.stringify({
      status: "error",
      message: error.toString()
    })).setMimeType(ContentService.MimeType.JSON);
  }
}
