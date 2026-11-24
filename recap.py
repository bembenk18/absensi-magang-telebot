import asyncio
import html
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from telegram.error import RetryAfter

import config
import database
import utils

logger = logging.getLogger(__name__)

DAILY_RECAP_JOB_NAME = "daily_recap"


def is_recap_due(
    now: datetime,
    enabled: bool,
    hour: int,
    minute: int,
    skip_weekends: bool,
    already_sent: bool,
) -> bool:
    """Menentukan apakah rekap harian otomatis sudah waktunya dikirim."""
    if not enabled or already_sent:
        return False
    if skip_weekends and now.weekday() in (5, 6):
        return False
    return (now.hour, now.minute) >= (hour, minute)


def _merge_entry(student: Optional[Dict[str, Any]], row: Optional[Dict[str, Any]], is_non_student: bool = False) -> Dict[str, Any]:
    src_s = student or {}
    src_r = row or {}
    return {
        "telegram_id": src_r.get("telegram_id", src_s.get("telegram_id")),
        "full_name": src_r.get("full_name") or src_s.get("full_name") or "Tanpa Nama",
        "school": src_r.get("school") or src_s.get("school") or "-",
        "nisn": src_r.get("nisn") or src_s.get("nisn") or src_s.get("nik") or "-",
        "check_in_office": src_r.get("check_in_office") or "-",
        "check_in_time": src_r.get("check_in_time") or "-",
        "check_out_office": src_r.get("check_out_office") or "-",
        "check_out_time": src_r.get("check_out_time") or "-",
        "activity": src_r.get("activity") or "-",
        "status": src_r.get("status") or "Belum Absen",
        "is_non_student": is_non_student,
    }


def _classify_row_bucket(row: Dict[str, Any]) -> str:
    status = str(row.get("status") or "").strip()
    if status == "Hadir":
        out_time = str(row.get("check_out_time") or "").strip()
        if out_time and out_time != "-":
            return "hadir_lengkap"
        return "hadir_belum_pulang"
    if status == "Izin":
        return "izin"
    if status == "Sakit":
        return "sakit"
    if status == "Libur":
        return "libur"
    return "belum_absen"


def classify_daily_attendance(
    students: List[Dict[str, Any]],
    attendance_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Mengelompokkan seluruh siswa approved dan baris absensi hari tersebut ke kategori:
    - hadir_lengkap
    - hadir_belum_pulang
    - izin
    - sakit
    - libur
    - belum_absen
    Baris absensi milik non-siswa (mis. admin) tetap diproses dengan flag is_non_student=True.
    """
    buckets: Dict[str, List[Dict[str, Any]]] = {
        "hadir_lengkap": [],
        "hadir_belum_pulang": [],
        "izin": [],
        "sakit": [],
        "libur": [],
        "belum_absen": [],
    }

    att_by_id: Dict[Any, Dict[str, Any]] = {}
    for row in attendance_rows:
        tid = row.get("telegram_id")
        if tid is not None:
            att_by_id[tid] = row

    seen_student_ids = set()
    for student in students:
        tid = student.get("telegram_id")
        seen_student_ids.add(tid)
        row = att_by_id.get(tid)
        if row is None:
            buckets["belum_absen"].append(_merge_entry(student, None, is_non_student=False))
        else:
            bucket = _classify_row_bucket(row)
            buckets[bucket].append(_merge_entry(student, row, is_non_student=False))

    non_student_count = 0
    for row in attendance_rows:
        tid = row.get("telegram_id")
        if tid not in seen_student_ids:
            non_student_count += 1
            bucket = _classify_row_bucket(row)
            buckets[bucket].append(_merge_entry(None, row, is_non_student=True))

    counts = {
        "total_students": len(students),
        "hadir_lengkap": len(buckets["hadir_lengkap"]),
        "hadir_belum_pulang": len(buckets["hadir_belum_pulang"]),
        "izin": len(buckets["izin"]),
        "sakit": len(buckets["sakit"]),
        "libur": len(buckets["libur"]),
        "belum_absen": len(buckets["belum_absen"]),
        "non_student_records": non_student_count,
    }

    return {
        "total_students": len(students),
        **buckets,
        "counts": counts,
    }


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else "-"))


def _truncate(text: str, limit: int = 160) -> str:
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3] + "..."


_HTML_ENTITY_RE = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);")


def _tokenize_html_line(line: str) -> List[tuple[str, str, str]]:
    """Memecah satu baris HTML menjadi token tag buka/tutup, entitas &...;, dan karakter."""
    tokens: List[tuple[str, str, str]] = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "<":
            end = line.find(">", i)
            if end != -1:
                raw = line[i : end + 1]
                if raw.startswith("</"):
                    inner = raw[2:-1].strip()
                    tag_name = inner.split()[0].lower() if inner else ""
                    tokens.append(("close", tag_name, raw))
                elif raw.endswith("/>"):
                    tokens.append(("text", "", raw))
                else:
                    inner = raw[1:-1].strip()
                    tag_name = inner.split()[0].lower() if inner else ""
                    if tag_name:
                        tokens.append(("open", tag_name, raw))
                    else:
                        tokens.append(("text", "", raw))
                i = end + 1
                continue
        elif ch == "&":
            match = _HTML_ENTITY_RE.match(line, i)
            if match:
                tokens.append(("text", "", match.group(0)))
                i = match.end()
                continue
        tokens.append(("text", "", ch))
        i += 1
    return tokens


def _split_html_line(line: str, max_len: int) -> List[str]:
    """Memecah satu baris HTML yang melebihi max_len tanpa memotong tag atau entitas HTML."""
    tokens = _tokenize_html_line(line)
    sub_chunks: List[str] = []
    open_stack: List[tuple[str, str]] = []
    buf: List[str] = []
    buf_len = 0
    has_payload = False

    def _closing_suffix() -> str:
        return "".join(f"</{name}>" for name, _ in reversed(open_stack))

    def _closing_len() -> int:
        return sum(len(name) + 3 for name, _ in open_stack)

    def _flush() -> None:
        nonlocal buf, buf_len, has_payload
        if not buf:
            return
        sub_chunks.append("".join(buf) + _closing_suffix())
        buf = [raw_open for _, raw_open in open_stack]
        buf_len = sum(len(s) for s in buf)
        has_payload = False

    for kind, tag_name, raw in tokens:
        if kind == "open":
            close_tag_len = len(tag_name) + 3
            if has_payload and (buf_len + len(raw) + _closing_len() + close_tag_len + 8 > max_len):
                _flush()
            buf.append(raw)
            buf_len += len(raw)
            open_stack.append((tag_name, raw))
            has_payload = True
        elif kind == "close":
            pop_idx = None
            for idx in range(len(open_stack) - 1, -1, -1):
                if open_stack[idx][0] == tag_name:
                    pop_idx = idx
                    break
            if pop_idx is not None:
                if has_payload and (buf_len + len(raw) + _closing_len() - (len(tag_name) + 3) > max_len):
                    _flush()
                open_stack.pop(pop_idx)
                buf.append(raw)
                buf_len += len(raw)
                has_payload = True
        else:
            if has_payload and (buf_len + len(raw) + _closing_len() > max_len):
                _flush()
            buf.append(raw)
            buf_len += len(raw)
            has_payload = True

    if has_payload:
        sub_chunks.append("".join(buf) + _closing_suffix())

    return sub_chunks


def _split_lines_to_chunks(lines: List[str], max_len: int) -> List[str]:
    if max_len < 64:
        max_len = 64
    chunks: List[str] = []
    current_lines: List[str] = []
    current_len = 0

    for line in lines:
        if len(line) > max_len:
            # Flush existing buffer first
            if current_lines:
                chunks.append("\n".join(current_lines))
                current_lines = []
                current_len = 0
            sub_chunks = _split_html_line(line, max_len)
            if sub_chunks:
                chunks.extend(sub_chunks[:-1])
                tail = sub_chunks[-1]
                current_lines = [tail]
                current_len = len(tail)
            continue

        added_len = len(line) if not current_lines else len(line) + 1
        if current_len + added_len > max_len:
            chunks.append("\n".join(current_lines))
            current_lines = [line]
            current_len = len(line)
        else:
            current_lines.append(line)
            current_len += added_len

    if current_lines:
        chunks.append("\n".join(current_lines))

    return chunks or [""]


def format_daily_recap_messages(
    date_str: str,
    summary: Dict[str, Any],
    now: Optional[datetime] = None,
    max_len: int = 4096,
) -> List[str]:
    """
    Memformat ringkasan rekap absensi harian menjadi satu atau beberapa pesan HTML
    yang aman (html.escape) dengan panjang masing-masing <= max_len.
    """
    if "hadir_lengkap" not in summary and "students" in summary:
        summary = classify_daily_attendance(
            summary.get("students", []),
            summary.get("attendance_rows", []),
        )

    counts = summary.get("counts", {})
    total_students = counts.get("total_students", summary.get("total_students", 0))
    ts_str = now.strftime("%H:%M:%S") if now else utils.get_now().strftime("%H:%M:%S")

    lines: List[str] = [
        f"📊 <b>REKAP ABSENSI HARIAN</b>",
        f"📅 Tanggal: <b>{_esc(date_str)}</b>",
        f"🕒 Waktu Rekap: <code>{_esc(ts_str)}</code>",
        f"👥 Total Siswa Terdaftar (Approved): <b>{total_students}</b>",
        "",
        "<b>Ringkasan Kehadiran:</b>",
        f"• ✅ Hadir Lengkap (Masuk &amp; Pulang): <b>{len(summary.get('hadir_lengkap', []))}</b>",
        f"• 🟡 Hadir (Belum Pulang): <b>{len(summary.get('hadir_belum_pulang', []))}</b>",
        f"• 📝 Izin: <b>{len(summary.get('izin', []))}</b>",
        f"• 🤒 Sakit: <b>{len(summary.get('sakit', []))}</b>",
        f"• 🏖️ Libur: <b>{len(summary.get('libur', []))}</b>",
        f"• ❌ Belum Absen: <b>{len(summary.get('belum_absen', []))}</b>",
    ]

    if total_students == 0 and not any(
        summary.get(k) for k in ("hadir_lengkap", "hadir_belum_pulang", "izin", "sakit", "libur")
    ):
        lines.extend(["", "<i>Belum ada siswa approved maupun data absensi pada tanggal ini.</i>"])
        return _split_lines_to_chunks(lines, max_len)

    def _tag(item: Dict[str, Any]) -> str:
        return " <i>(Non-Siswa)</i>" if item.get("is_non_student") else ""

    sections = [
        ("✅ Hadir Lengkap", summary.get("hadir_lengkap", [])),
        ("🟡 Hadir (Belum Pulang)", summary.get("hadir_belum_pulang", [])),
        ("📝 Izin", summary.get("izin", [])),
        ("🤒 Sakit", summary.get("sakit", [])),
        ("🏖️ Libur", summary.get("libur", [])),
        ("❌ Belum Absen", summary.get("belum_absen", [])),
    ]

    for title, items in sections:
        if not items:
            continue
        lines.append("")
        lines.append(f"<b>{title} ({len(items)}):</b>")
        for idx, item in enumerate(items, start=1):
            name = _esc(item.get("full_name", "-"))
            school = _esc(item.get("school", "-"))
            tag = _tag(item)
            if title == "✅ Hadir Lengkap":
                in_t = _esc(item.get("check_in_time", "-"))
                out_t = _esc(item.get("check_out_time", "-"))
                office = _esc(item.get("check_in_office", "-"))
                act_raw = item.get("activity") or "-"
                act = _esc(_truncate(act_raw, 120))
                lines.append(
                    f"{idx}. <b>{name}</b> ({school}){tag} — {office} | Masuk <code>{in_t}</code>, Pulang <code>{out_t}</code> | <i>{act}</i>"
                )
            elif title == "🟡 Hadir (Belum Pulang)":
                in_t = _esc(item.get("check_in_time", "-"))
                office = _esc(item.get("check_in_office", "-"))
                lines.append(
                    f"{idx}. <b>{name}</b> ({school}){tag} — {office} | Masuk <code>{in_t}</code> (Belum pulang)"
                )
            else:
                lines.append(f"{idx}. <b>{name}</b> ({school}){tag}")

    return _split_lines_to_chunks(lines, max_len)


async def _send_with_retry(coro_factory) -> None:
    try:
        await coro_factory()
    except RetryAfter as exc:
        delay = float(getattr(exc, "retry_after", 1.0)) + 0.5
        await asyncio.sleep(min(delay, 10.0))
        await coro_factory()


async def send_daily_recap(
    bot: Any,
    date_str: Optional[str] = None,
    force: bool = False,
    now: Optional[datetime] = None,
    mark_sent: Optional[bool] = None,
    recipient_ids: Optional[Iterable[int]] = None,
) -> int:
    """
    Menyusun dan mengirimkan rekap absensi harian ke penerima yang dikonfigurasi.
    - Jika force=False, otomatis melewatkan pengiriman bila tanggal tersebut sudah tercatat di scheduled_job_logs.
    - Mencatat ke scheduled_job_logs hanya jika sent_count > 0 dan mark_sent aktif.
    """
    current_now = now or utils.get_now()
    target_date = date_str or current_now.strftime("%Y-%m-%d")

    if not force and database.has_scheduled_job_run(DAILY_RECAP_JOB_NAME, target_date):
        return 0

    if recipient_ids is not None:
        recipients = [int(r) for r in recipient_ids]
    else:
        configured = config.RECAP_RECIPIENT_IDS or config.ADMIN_IDS
        recipients = sorted(int(r) for r in configured)

    if not recipients:
        logger.warning("Tidak ada penerima rekap harian (RECAP_RECIPIENT_IDS/ADMIN_IDS kosong).")
        return 0

    raw_summary = database.get_daily_recap_summary(target_date)
    classified = classify_daily_attendance(
        raw_summary.get("students", []),
        raw_summary.get("attendance_rows", []),
    )
    messages = format_daily_recap_messages(target_date, classified, now=current_now)

    csv_path: Optional[str] = None
    if config.RECAP_SEND_CSV:
        try:
            csv_path = utils.generate_csv_report(
                raw_summary.get("attendance_rows", []),
                filename_prefix=f"rekap_harian_{target_date}",
            )
        except Exception as exc:
            logger.error("Gagal membuat file CSV rekap harian %s: %s", target_date, exc)
            csv_path = None

    sent_count = 0
    try:
        for idx, chat_id in enumerate(recipients):
            try:
                for msg_text in messages:
                    await _send_with_retry(
                        lambda m=msg_text, cid=chat_id: bot.send_message(
                            chat_id=cid,
                            text=m,
                            parse_mode="HTML",
                        )
                    )
                if csv_path and Path(csv_path).is_file():
                    async def _send_csv(cid=chat_id, p=csv_path):
                        with open(p, "rb") as f:
                            await bot.send_document(
                                chat_id=cid,
                                document=f,
                                filename=Path(p).name,
                                caption=f"📎 Lampiran CSV Rekap Harian ({html.escape(target_date)})",
                                parse_mode="HTML",
                            )
                    await _send_with_retry(_send_csv)
                sent_count += 1
            except Exception as exc:
                logger.warning("Gagal mengirim rekap harian ke penerima %s: %s", chat_id, exc)

            if idx < len(recipients) - 1:
                await asyncio.sleep(0.05)
    finally:
        if csv_path:
            try:
                Path(csv_path).unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Gagal menghapus CSV sementara %s: %s", csv_path, exc)

    should_mark = (not force) if mark_sent is None else mark_sent
    if should_mark and sent_count > 0:
        database.mark_scheduled_job_run(
            DAILY_RECAP_JOB_NAME,
            target_date,
            current_now.strftime("%Y-%m-%d %H:%M:%S"),
            sent_count,
        )

    return sent_count
