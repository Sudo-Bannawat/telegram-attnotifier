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
LEAVE_FORM_URL = f"{BASE_URL}/popup2.php?name=tkk6&file=readtkk6_1&id={{leave_id}}"

USER = os.getenv("MYOFFICE_USER")
PASS = os.getenv("MYOFFICE_PASS")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RESET_NOTIFICATION_STATE = os.getenv("RESET_NOTIFICATION_STATE") == "true"

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


def is_leave_row(cols: list[str]) -> bool:
    """Reject dashboard/statistics rows such as fiscal year 2570."""
    if not cols or not cols[0].strip().isdigit():
        return False
    first_value = int(cols[0].strip())
    if 2400 <= first_value <= 2700:  # Thai Buddhist fiscal/calendar year
        return False
    return sum(bool(ISO_DATE.fullmatch(cell.strip())) for cell in cols) >= 1


def parse_leave_page(html: str):
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
        raise ValueError(
            "No dated leave records found; check MyOffice account permission or page layout"
        )
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
    return {
        "id": row[0],
        "activity_type": row[1] if len(row) > 1 else "",
        "leave_type": row[1] if len(row) > 1 else "",
        "phone": row[5] if len(row) > 5 else "",
        "start_date": row[6] if len(row) > 6 else "",
        "end_date": row[7] if len(row) > 7 else "",
        "record_date": row[9] if len(row) > 9 else "",
        "requester_name": row[10] if len(row) > 10 else "",
        "reason": row[12] if len(row) > 12 else "",
        "school": row[4] if len(row) > 4 else "",
        "address": row[17] if len(row) > 17 else "",
        "status": row[21] if len(row) > 21 else "รอดำเนินการ",
    }


def leave_message(leave: dict, heading: str):
    return (
        f"<b>{heading}</b>\n"
        f"<blockquote>🔖 <b>เลขที่ใบลา:</b> #{escape(leave['id'])}\n"
        f"👤 <b>ผู้ยื่น:</b> {escape(leave['requester_name'])}\n"
        f"🏷️ <b>ประเภท:</b> {escape(leave['leave_type'])}\n"
        f"📅 <b>วันลา:</b> {escape(leave['start_date'])} — {escape(leave['end_date'])}\n"
        f"💬 <b>เรื่อง:</b> {escape(leave['activity_type'])}\n"
        f"📝 <b>เหตุผล:</b> {escape(leave['reason'])}\n"
        f"🏫 <b>หน่วยงาน:</b> {escape(leave['school'])}\n"
        f"📞 <b>โทร:</b> {escape(leave['phone'])}\n"
        f"📍 <b>ที่อยู่:</b> {escape(leave['address'])}\n"
        f"⏳ <b>สถานะ:</b> {escape(leave['status'])}</blockquote>\n"
        f"<i>บันทึกเมื่อ {escape(leave['record_date'])}</i>"
    )


def is_after_today(leave: dict) -> bool:
    try:
        leave_date = date.fromisoformat(leave["start_date"])
    except ValueError:
        print(f"Skip leave #{leave['id']}: invalid start date {leave['start_date']!r}")
        return False
    bangkok = timezone(timedelta(hours=7))
    return leave_date > datetime.now(bangkok).date()


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
    leaves = [leave_from_row(row) for row in parse_leave_page(resp.text)]

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
        if status.lower() in ["อนุญาติ", "approved", "accept"]:
            decision = "✅ อนุญาติ"
        elif status.lower() in ["ปฏิเสธ", "reject", "rejected"]:
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
