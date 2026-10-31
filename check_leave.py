# check_leave.py - GitHub Actions runnable script
"""Monitor MyOffice leave requests and send Telegram notifications.

Features (as requested):
* Excludes the "รหัสหน่วยงาน" column.
* Includes the "สถานะ" and "ไฟล์แนบ" columns.
* When a request is first seen, a detailed message is sent.
* When the same request later changes status to "อนุญาติ" or "ปฏิเสธ",
  a short reply is sent indicating the decision.

The script is deliberately small – it uses only the standard library and
`requests`/`beautifulsoup4` (both available on the default GitHub‑Actions
Python image). No extra configuration is required beyond the four secrets:
`MYOFFICE_USER`, `MYOFFICE_PASS`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
"""

import os
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration via environment (GitHub Secrets)
# ---------------------------------------------------------------------------
BASE_URL = "http://202.139.198.149/myoffice/ubonrat"
LOGIN_URL = f"{BASE_URL}/index.php?name=user&file=login"
LEAVE_CATEGORY = os.getenv("MYOFFICE_LEAVE_CATEGORY", "187")
LEAVE_URL = (
    f"{BASE_URL}/index.php?name=tkk6&op=tkk6_read&category={LEAVE_CATEGORY}"
)
CANCELLATION_URL = (
    f"{BASE_URL}/index.php?name=tkknola&op=tkknola_read&category={LEAVE_CATEGORY}"
)
LEAVE_FORM_URL = f"{BASE_URL}/popup2.php?name=tkk6&file=readtkk6_1&id={{leave_id}}"

USER = os.getenv("MYOFFICE_USER")
PASS = os.getenv("MYOFFICE_PASS")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RESET_NOTIFICATION_STATE = os.getenv("RESET_NOTIFICATION_STATE") == "true"
DAILY_LEAVE_REPORT = os.getenv("DAILY_LEAVE_REPORT") == "true"

if not all([USER, PASS, BOT_TOKEN, CHAT_ID]):
    print("⚠️ Missing one or more required environment variables.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Helper: send a Telegram message (HTML formatting)
# ---------------------------------------------------------------------------
def send_telegram(
    text: str,
    reply_to_message_id: int | None = None,
    reply_markup: dict | None = None,
):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_to_message_id:
        payload["reply_parameters"] = json.dumps(
            {"message_id": reply_to_message_id}, ensure_ascii=False
        )
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    r = requests.post(url, data=payload, timeout=10)
    r.raise_for_status()
    return r.json()

# ---------------------------------------------------------------------------
# Helper: login and keep a session with cookies
# ---------------------------------------------------------------------------
def login() -> requests.Session:
    s = requests.Session()
    # The admin login form expects fields "username" and "password" (observed
    # from earlier inspection). Adjust if the real names differ.
    data = {"username": USER, "password": PASS}
    resp = s.post(LOGIN_URL, data=data, timeout=15)
    resp.raise_for_status()
    # Login endpoint returns the authenticated dashboard and PHPSESSID cookie.
    return s

# ---------------------------------------------------------------------------
# Helper: parse the leave table – returns a list of dicts (one per row)
# ---------------------------------------------------------------------------
ISO_DATE = re.compile(r"^20\d{2}-\d{2}-\d{2}$")
THAI_MONTHS = {
    "ม.ค.": 1, "ม.ค": 1, "มกราคม": 1, "มค": 1,
    "ก.พ.": 2, "ก.พ": 2, "กุมภาพันธ์": 2, "กพ": 2,
    "มี.ค.": 3, "มี.ค": 3, "มีนาคม": 3, "มีค": 3,
    "เม.ย.": 4, "เม.ย": 4, "เมษายน": 4, "เมย": 4,
    "พ.ค.": 5, "พ.ค": 5, "พฤษภาคม": 5, "พค": 5,
    "มิ.ย.": 6, "มิ.ย": 6, "มิถุนายน": 6, "มิย": 6,
    "ก.ค.": 7, "ก.ค": 7, "กรกฎาคม": 7, "กค": 7,
    "ส.ค.": 8, "ส.ค": 8, "สิงหาคม": 8, "สค": 8,
    "ก.ย.": 9, "ก.ย": 9, "กันยายน": 9, "กย": 9,
    "ต.ค.": 10, "ต.ค": 10, "ตุลาคม": 10, "ตค": 10,
    "พ.ย.": 11, "พ.ย": 11, "พฤศจิกายน": 11, "พย": 11,
    "ธ.ค.": 12, "ธ.ค": 12, "ธันวาคม": 12, "ธค": 12,
}
THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
THAI_DATE = re.compile(
    r"([0-9๐-๙]{1,2})\s*(?:\b)?([ก-ฮA-Za-z\s\.]+?)\s*(?:พ\.?\s*ศ\.?\s*)?([0-9๐-๙]{4})"
)


def normalize_thai_month(value: str) -> str:
    return re.sub(r"[\s\.]+", "", value or "")


def is_leave_row(cols: list[str]) -> bool:
    """Reject dashboard/statistics rows such as fiscal year 2570."""
    if not cols or not cols[0].strip().isdigit():
        return False
    first_value = int(cols[0].strip())
    if 2400 <= first_value <= 2700:  # Thai Buddhist fiscal/calendar year
        return False
    return sum(bool(ISO_DATE.fullmatch(cell.strip())) for cell in cols) >= 1


def thai_date_to_iso(value: str) -> str:
    if not value:
        return ""
    if ISO_DATE.fullmatch(value.strip()):
        return value.strip()

    match = THAI_DATE.search(value)
    if not match:
        return ""

    day, month, year = match.groups()
    month_key = normalize_thai_month(month)
    month_num = None

    for key, month_value in THAI_MONTHS.items():
        if normalize_thai_month(key) == month_key:
            month_num = month_value
            break

    if month_num is None:
        return ""

    try:
        d = int(day.translate(THAI_DIGITS))
        y = int(year.translate(THAI_DIGITS)) - 543
        return date(y, month_num, d).isoformat()
    except ValueError:
        return ""


def leave_from_form(leave_id: str, topic: str, status: str, html: str):
    """Parse current MyOffice popup form when the list is rendered as dashboard cards."""
    text = " ".join(BeautifulSoup(html, "html.parser").stripped_strings)

    def capture(pattern: str) -> str:
        match = re.search(pattern, text)
        return match.group(1).strip() if match else ""

    start = capture(r"ตั้งแต่วันที่\s*(.*?)\s*ถึงวันที่") or capture(r"ตั้งแต่\s*(.*?)\s*ถึงวันที่")
    end = capture(r"ถึงวันที่\s*(.*?)\s*มีกำหนด") or capture(r"ถึง\s*(.*?)\s*มีกำหนด")
    record = capture(r"วันที่\s*(.*?)\s*เรื่อง") or capture(r"วันที่\s*(.*?)\s*เรื่องการลา")
    if not start and record:
        start = record

    start_iso = thai_date_to_iso(start)
    record_iso = thai_date_to_iso(record)
    if not start_iso and record_iso:
        start_iso = record_iso

    return {
        "id": leave_id,
        "activity_type": topic,
        "leave_type": capture(r"ขอลา\s*(.*?)\s*เนื่องจาก"),
        "topic": topic,
        "phone": capture(r"โทรศัพท์\s*(.*?)\s*ขอแสดงความนับถือ"),
        "start_date": start_iso,
        "end_date": thai_date_to_iso(end) or start_iso,
        "record_date": record_iso,
        "requester_name": capture(r"ข้าพเจ้า\s*(.*?)\s*ตำแหน่ง"),
        "reason": capture(r"เนื่องจาก\s*(.*?)\s*ตั้งแต่วันที่"),
        "school": capture(r"สังกัด\s*(.*?)\s*ขอลา"),
        "address": capture(r"ระหว่างลาติดต่อข้าพเจ้าได้ที่\s*(.*?)\s*โทรศัพท์"),
        "status": status,
        "position": capture(r"ตำแหน่ง\s*(?:ตำแหน่ง\s*)?(.*?)\s*สังกัด"),
    }


def parse_dashboard_leaves(html: str, session: requests.Session):
    """Read the current tkk6 dashboard, whose rows link each leave to a popup form."""
    soup = BeautifulSoup(html, "html.parser")
    leaves = []
    seen = set()
    for tr in soup.find_all("tr"):
        link = next((a.get("href", "") for a in tr.find_all("a") if "readtkk6_1&id=" in a.get("href", "")), "")
        match = re.search(r"readtkk6_1&id=(\d+)", link)
        if not match:
            continue
        cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["td", "th"])]
        # Ignore outer layout rows; leave cards have six direct cells.
        if not 5 <= len(cells) <= 7:
            continue
        if match.group(1) in seen:
            continue
        seen.add(match.group(1))
        detail = session.get(LEAVE_FORM_URL.format(leave_id=match.group(1)), timeout=15)
        detail.raise_for_status()
        leaves.append(leave_from_form(match.group(1), cells[1], cells[-2] if len(cells) > 2 else "รอดำเนินการ", detail.text))
    if not leaves:
        raise ValueError("Leave dashboard contained no form links")
    return leaves


def parse_leave_page(html: str, session: requests.Session):
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        raise ValueError("Leave table not found on page")

    candidates = []
    for table in tables:
        rows = []
        for tr in table.find_all("tr"):
            cols = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if is_leave_row(cols):
                rows.append(cols)
        if rows:
            candidates.append(rows)
    if not candidates:
        return parse_dashboard_leaves(html, session)
    return max(candidates, key=len)

# ---------------------------------------------------------------------------
# State persistence – a tiny JSON file storing the last known status per ID.
# ---------------------------------------------------------------------------
STATE_FILE = Path(__file__).with_name("leave_state.json")
if STATE_FILE.exists():
    with open(STATE_FILE, "r", encoding="utf-8-sig") as f:  # utf-8-sig strips BOM if present
        content = f.read().strip()
        state = json.loads(content) if content else {}
else:
    state = {}


def save_state():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def state_entry(leave_id: str):
    """Read old string state safely; new state also stores Telegram message ID."""
    entry = state.get(leave_id, {})
    if isinstance(entry, str):
        return {"status": entry}
    return entry


def leave_form_button(leave_id: str, leave_type: str):
    """Return a Telegram button only for the three leave-form types."""
    supported_types = {"ป่วย", "ลาคลอดบุตร", "กิจส่วนตัว"}
    if leave_type.strip() not in supported_types:
        return None
    return {
        "inline_keyboard": [[{
            "text": "📄 เปิดแบบฟอร์มใบลา",
            "url": LEAVE_FORM_URL.format(leave_id=leave_id),
        }]]
    }


def leave_from_row(row: list[str]):
    """Map `web_tkk6` columns used by the current MyOffice leave list."""
    if isinstance(row, dict):
        return row
    return {
        "id": row[0],
        "activity_type": row[1] if len(row) > 1 else "",
        "leave_type": row[1] if len(row) > 1 else "",
        "topic": row[8] if len(row) > 8 else "",
        "phone": row[5] if len(row) > 5 else "",
        "start_date": row[6] if len(row) > 6 else "",
        "end_date": row[7] if len(row) > 7 else "",
        "record_date": row[9] if len(row) > 9 else "",
        "requester_name": row[10] if len(row) > 10 else "",
        "reason": row[12] if len(row) > 12 else "",
        "school": row[4] if len(row) > 4 else "",
        "address": row[17] if len(row) > 17 else "",
        "status": row[20] if len(row) > 20 else "รอดำเนินการ",
        "position": row[24] if len(row) > 24 else "",
    }


def leave_message(leave: dict, heading: str):
    return (
        f"<b>{heading}</b>\n"
        f"<blockquote>🔖 <b>เลขที่ใบลา:</b> #{escape(leave['id'])}\n"
        f"👤 <b>ผู้ยื่น:</b> {escape(leave['requester_name'])}\n"
        f"🏷️ <b>ประเภท:</b> {escape(leave['leave_type'])}\n"
        f"📅 <b>วันลา:</b> {escape(leave['start_date'])} — {escape(leave['end_date'])}\n"
        f"💬 <b>เรื่อง:</b> {escape(leave['topic'])}\n"
        f"📝 <b>เหตุผล:</b> {escape(leave['reason'])}\n"
        f"🏫 <b>หน่วยงาน:</b> {escape(leave['school'])}\n"
        f"📞 <b>โทร:</b> {escape(leave['phone'])}\n"
        f"📍 <b>ที่อยู่:</b> {escape(leave['address'])}\n"
        f"⏳ <b>สถานะ:</b> {escape(leave['status'])}</blockquote>\n"
        f"<i>บันทึกเมื่อ {escape(leave['record_date'])}</i>"
    )


def is_after_today(leave: dict) -> bool:
    start_date = leave.get("start_date")
    if not start_date:
        print(f"⚠️ Skip leave #{leave.get('id', '?')}: missing start date")
        return False
    try:
        leave_date = date.fromisoformat(start_date)
    except ValueError:
        print(f"Skip leave #{leave.get('id', '?')}: invalid start date {start_date!r}")
        return False
    bangkok = timezone(timedelta(hours=7))
    return leave_date > datetime.now(bangkok).date()


def bangkok_today() -> date:
    return datetime.now(timezone(timedelta(hours=7))).date()


def status_kind(status: str) -> str:
    value = status.strip().lower()
    if any(word in value for word in ("ยกเลิก", "cancel")):
        return "cancelled"
    if value in {"อนุญาต", "อนุญาติ", "อนุมัติ", "approved", "accept"}:
        return "approved"
    if value in {"ไม่อนุญาต", "ไม่อนุญาติ", "ปฏิเสธ", "reject", "rejected"}:
        return "rejected"
    return "pending"


def thai_date(value: date) -> str:
    return f"{value.day:02d}/{value.month:02d}/{value.year + 543}"


def daily_report_markup(leaves: list[dict], offset: int) -> dict:
    buttons = []
    for index, leave in enumerate(leaves, start=offset + 1):
        buttons.append([{
            "text": f"ดูแบบเต็ม · {index}",
            "url": LEAVE_FORM_URL.format(leave_id=leave["id"]),
        }])
    return {"inline_keyboard": buttons}


def send_daily_report(leaves: list[dict], heading: str, report_date: date):
    """Send ordered report chunks; Telegram supports buttons below, not inline text."""
    for offset in range(0, len(leaves), 20):
        chunk = leaves[offset:offset + 20]
        lines = []
        for index, leave in enumerate(chunk, start=offset + 1):
            lines.append(
                f"<b>{index}.</b> {escape(leave['requester_name'])} - "
                f"{escape(leave['position'])}\n"
                f"{escape(leave['topic'])}\n"
                f"<i>เหตุผลการลา:</i> {escape(leave['reason'])}"
            )
        title = heading if offset == 0 else f"{heading} (ต่อ)"
        text = f"<b>{title}</b>\n📅 {thai_date(report_date)}\n\n" + "\n\n".join(lines)
        send_telegram(text, reply_markup=daily_report_markup(chunk, offset))


def cancelled_leave_ids(session: requests.Session) -> set[str]:
    """Get original leave IDs from the cancellation register (`tkknola.edit`)."""
    response = session.get(CANCELLATION_URL, timeout=15)
    response.raise_for_status()
    cancellation_rows = parse_leave_page(response.text)
    return {
        row[5].strip()
        for row in cancellation_rows
        if len(row) > 5 and row[5].strip().isdigit()
    }


def run_daily_leave_report(leaves: list[dict], cancelled_ids: set[str]):
    today = bangkok_today()
    report_key = today.isoformat()
    reports = state.setdefault("_daily_reports", {})
    if report_key in reports:
        print(f"Daily report already sent for {report_key}")
        return

    today_leaves = [
        leave for leave in leaves
        if leave["start_date"] == report_key and leave["id"] not in cancelled_ids
    ]
    approved = [leave for leave in today_leaves if status_kind(leave["status"]) == "approved"]
    rejected = [leave for leave in today_leaves if status_kind(leave["status"]) == "rejected"]
    approved.sort(key=lambda leave: (leave["requester_name"], int(leave["id"])))
    rejected.sort(key=lambda leave: (leave["requester_name"], int(leave["id"])))

    if approved:
        send_daily_report(approved, "วันนี้มีบุคลากรแจ้งลา", today)
    if rejected:
        send_daily_report(rejected, "ผู้กรอกแบบฟอร์มขอลาไม่ได้รับอนุญาต", today)

    reports[report_key] = {
        "approved": len(approved),
        "rejected": len(rejected),
    }
    save_state()
    print(f"Daily report: approved={len(approved)}, rejected={len(rejected)}")


def save_notified_leave(leave: dict, heading: str):
    result = send_telegram(
        leave_message(leave, heading),
        reply_markup=leave_form_button(leave["id"], leave["leave_type"]),
    )
    state[leave["id"]] = {
        "status": leave["status"],
        "telegram_message_id": result["result"]["message_id"],
    }
    save_state()

# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------
def main():
    global state
    session = login()
    resp = session.get(LEAVE_URL, timeout=15)
    resp.raise_for_status()
    leaves = [leave_from_row(row) for row in parse_leave_page(resp.text, session)]

    if RESET_NOTIFICATION_STATE:
        # Manual reset: state must contain only future leave dates, regardless
        # of approval result, so later decisions stay threaded to these messages.
        future_leaves = sorted(
            (leave for leave in leaves if is_after_today(leave)),
            key=lambda leave: (leave["start_date"], int(leave["id"])),
        )
        state = {}
        save_state()
        print(f"Reset: sending {len(future_leaves)} leave notification(s) after today")
        for leave in future_leaves:
            save_notified_leave(leave, "📚 บันทึกใบลาหลังวันนี้")
        return

    if DAILY_LEAVE_REPORT:
        # Fail closed: no report is safer than reporting leave already cancelled.
        run_daily_leave_report(leaves, cancelled_leave_ids(session))
        return

    # Normal scheduled check handles newest leave only.
    leave = leaves[0]
    ID = leave["id"]
    status = leave["status"]

    # -------------------------------------------------------------------
    # 1️⃣ New request handling
    # -------------------------------------------------------------------
    entry = state_entry(ID)
    if ID not in state:
        save_notified_leave(leave, "📝 มีใบลาใหม่")
        return

    # -------------------------------------------------------------------
    # 2️⃣ Status change handling
    # -------------------------------------------------------------------
    prev_status = entry.get("status", "")
    if status != prev_status:
        # Only react to approval/rejection.
        decision = None
        if status_kind(status) == "approved":
            decision = "✅ อนุญาต"
        elif status_kind(status) == "rejected":
            decision = "❌ ปฏิเสธ"
        if decision:
            reply = f"<b>{decision}</b>\n<blockquote>ใบลาเลขที่ #{escape(ID)}</blockquote>"
            send_telegram(reply, entry.get("telegram_message_id"))
        # Update stored status regardless of whether we sent a reply.
        state[ID] = {**entry, "status": status}
        save_state()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # In a CI environment we still want the error logged.
        import traceback
        err = "".join(traceback.format_exception(None, e, e.__traceback__))
        print(f"⚡️ Error: {e}\n{err}")
        # Optionally notify admin of failure (omit to avoid spam).
        sys.exit(1)
