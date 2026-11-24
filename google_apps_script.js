/**
 * ============================================================================
 * GOOGLE APPS SCRIPT - SINKRONISASI ABSENSI TELEGRAM (HARDENED)
 * ============================================================================
 *
 * CARA MEMASANG DI GOOGLE SPREADSHEET:
 * 1. Buka spreadsheet baru di Google Sheets (sheets.new).
 * 2. Klik menu: Extensions (Ekstensi) > Apps Script.
 * 3. Hapus kode bawaan (myFunction), lalu paste seluruh kode di bawah ini.
 * 4. Buka Project Settings (ikon gerigi) > Script Properties > Add script property:
 *    - Property: WEBHOOK_SECRET
 *    - Value   : <isi secret acak minimal 32 karakter, SAMA dengan GOOGLE_SHEET_WEBHOOK_SECRET di .env>
 * 5. Klik tombol "Save" (ikon disket).
 * 6. Klik "Deploy" > "New deployment" > pilih "Web app":
 *    - Description: Webhook Absensi
 *    - Execute as: Me
 *    - Who has access: Anyone  <-- Endpoint tetap terautentikasi oleh HMAC-SHA256 WEBHOOK_SECRET.
 * 7. Salin "Web app URL" ke .env (GOOGLE_SHEET_WEBHOOK_URL=https://script.google.com/macros/s/.../exec)
 *    dan isi GOOGLE_SHEET_WEBHOOK_SECRET dengan nilai yang sama.
 */

var MAX_SKEW_SECONDS = 300; // Toleransi 5 menit untuk timestamp HMAC

function bytesToHex(bytes) {
  return bytes.map(function(b) {
    var v = (b < 0) ? b + 256 : b;
    return ("0" + v.toString(16)).slice(-2);
  }).join("");
}

function constantTimeEquals(a, b) {
  if (typeof a !== "string" || typeof b !== "string") {
    return false;
  }
  var maxLen = Math.max(a.length, b.length);
  var diff = a.length ^ b.length;
  for (var i = 0; i < maxLen; i++) {
    var ca = i < a.length ? a.charCodeAt(i) : 0;
    var cb = i < b.length ? b.charCodeAt(i) : 0;
    diff |= (ca ^ cb);
  }
  return diff === 0;
}

function sanitizeCell(val) {
  if (val === null || val === undefined) {
    return "-";
  }
  var s = String(val);
  if (s === "" || s === "-") {
    return "-";
  }
  if (/^[=+\-@\t\r\n\v\f]/.test(s)) {
    return "'" + s;
  }
  var trimmed = s.replace(/^[\s\u00a0\u200b\u200c\u200d\ufeff]+/, "");
  if (/^[=+\-@]/.test(trimmed) || /^"+[\s\u00a0\u200b\u200c\u200d\ufeff]*[=+\-@]/.test(trimmed)) {
    return "'" + s;
  }
  return s;
}

function sanitizeFilename(name) {
  var cleaned = String(name || "selfie.jpg").replace(/[^a-zA-Z0-9._-]/g, "_");
  if (!cleaned || cleaned === "." || cleaned === "..") {
    return "selfie.jpg";
  }
  return cleaned.slice(0, 120);
}

function jsonResponse(payload) {
  return ContentService.createTextOutput(JSON.stringify(payload))
    .setMimeType(ContentService.MimeType.JSON);
}

function verifyWebhookAuth(data, date, time, fullName, type) {
  var configuredSecret = PropertiesService.getScriptProperties().getProperty("WEBHOOK_SECRET") || "";
  if (!configuredSecret) {
    console.error("WEBHOOK_SECRET belum dikonfigurasi pada Script Properties.");
    return false;
  }

  var tsRaw = String(data.timestamp || "");
  var nonce = String(data.nonce || "");
  var signature = String(data.signature || "").toLowerCase();

  if (!/^\d+$/.test(tsRaw) || !nonce || !/^[a-f0-9]{64}$/.test(signature)) {
    return false;
  }

  var nowSec = Math.floor(Date.now() / 1000);
  var tsSec = parseInt(tsRaw, 10);
  if (Math.abs(nowSec - tsSec) > MAX_SKEW_SECONDS) {
    return false;
  }

  // Cegah replay attack menggunakan ScriptCache
  var cache = CacheService.getScriptCache();
  var cacheKey = "nonce_" + nonce;
  if (cache.get(cacheKey) !== null) {
    return false;
  }

  var canonical = tsRaw + ":" + nonce + ":" + date + ":" + time + ":" + fullName + ":" + type;
  var sigBytes = Utilities.computeHmacSha256Signature(canonical, configuredSecret, Utilities.Charset.UTF_8);
  var expectedSig = bytesToHex(sigBytes);

  if (!constantTimeEquals(signature, expectedSig)) {
    return false;
  }

  cache.put(cacheKey, "1", MAX_SKEW_SECONDS + 60);
  return true;
}

function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      return jsonResponse({ status: "error", message: "Invalid request payload" });
    }

    var data = JSON.parse(e.postData.contents);

    var rawDate = (data.date === undefined || data.date === null) ? "" : String(data.date);
    var rawTime = (data.time === undefined || data.time === null) ? "" : String(data.time);
    var rawFullName = (data.full_name === undefined || data.full_name === null) ? "" : String(data.full_name);
    var rawType = (data.type === undefined || data.type === null) ? "" : String(data.type);

    if (!verifyWebhookAuth(data, rawDate, rawTime, rawFullName, rawType)) {
      return jsonResponse({ status: "error", message: "Unauthorized" });
    }

    var date = rawDate || Utilities.formatDate(new Date(), "Asia/Jakarta", "yyyy-MM-dd");
    var time = rawTime || Utilities.formatDate(new Date(), "Asia/Jakarta", "HH:mm:ss");
    var fullName = rawFullName || "-";
    var school = (data.school === undefined || data.school === null || String(data.school) === "") ? "-" : String(data.school);
    var nisn = (data.nisn === undefined || data.nisn === null || String(data.nisn) === "") ? "-" : String(data.nisn);
    var type = rawType || "Masuk";
    var officeName = (data.office_name === undefined || data.office_name === null || String(data.office_name) === "") ? "Kantor" : String(data.office_name);
    var distance = (data.distance !== undefined && data.distance !== null && isFinite(Number(data.distance))) ? Math.round(Number(data.distance)) : 0;
    var activity = (data.activity === undefined || data.activity === null || String(data.activity) === "") ? "-" : String(data.activity);
    var imageB64 = (data.image_base64 === undefined || data.image_base64 === null) ? "" : String(data.image_base64);
    var rawFilename = data.filename || ("selfie_" + date + "_" + time.replace(/:/g, "") + ".jpg");
    var filename = sanitizeFilename(rawFilename);

    var fileUrl = "-";

    // 1. Simpan Foto Selfie ke Google Drive (Privat, tanpa ANYONE_WITH_LINK)
    if (imageB64) {
      var folderName = "Foto Absensi Siswa";
      var folders = DriveApp.getFoldersByName(folderName);
      var folder = folders.hasNext() ? folders.next() : DriveApp.createFolder(folderName);

      var decoded = Utilities.base64Decode(imageB64);
      var blob = Utilities.newBlob(decoded, "image/jpeg", filename);
      var file = folder.createFile(blob);

      file.setSharing(DriveApp.Access.PRIVATE, DriveApp.Permission.NONE);
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
        "Laporan Kegiatan",
        "Link Foto Drive"
      ]);

      var headerRange = sheet.getRange(1, 1, 1, 10);
      headerRange.setFontWeight("bold");
      headerRange.setBackground("#2563eb");
      headerRange.setFontColor("#ffffff");
      headerRange.setHorizontalAlignment("center");
      sheet.setFrozenRows(1);
    }

    // Validasi URL Drive sebelum membentuk HYPERLINK
    var photoCell = "-";
    if (fileUrl.indexOf("https://drive.google.com/") === 0) {
      var safeUrl = fileUrl.replace(/"/g, "");
      photoCell = '=HYPERLINK("' + safeUrl + '", "📸 Lihat Foto")';
    }

    // Tambah baris baru dengan proteksi formula injection
    sheet.appendRow([
      sanitizeCell(date),
      sanitizeCell(time),
      sanitizeCell(fullName),
      sanitizeCell(school),
      sanitizeCell(nisn),
      sanitizeCell(type),
      sanitizeCell(officeName),
      distance,
      sanitizeCell(activity),
      photoCell
    ]);

    var lastRow = sheet.getLastRow();
    sheet.getRange(lastRow, 1).setHorizontalAlignment("center"); // Tanggal
    sheet.getRange(lastRow, 2).setHorizontalAlignment("center"); // Waktu
    sheet.getRange(lastRow, 5).setNumberFormat("@").setHorizontalAlignment("center"); // NISN sebagai teks
    sheet.getRange(lastRow, 6).setHorizontalAlignment("center"); // Tipe
    sheet.getRange(lastRow, 8).setHorizontalAlignment("right");  // Jarak
    sheet.getRange(lastRow, 9).setHorizontalAlignment("left");   // Laporan Kegiatan
    sheet.getRange(lastRow, 10).setHorizontalAlignment("center"); // Link Foto

    return jsonResponse({
      status: "success",
      message: "Data absensi dan foto berhasil disimpan ke Google Sheets & Drive",
      file_url: fileUrl
    });

  } catch (error) {
    console.error("doPost internal error:", error);
    return jsonResponse({
      status: "error",
      message: "Internal processing error"
    });
  }
}
