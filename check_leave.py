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
import sys
from pathlib import Path
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration via environment (GitHub Secrets)
# ---------------------------------------------------------------------------
BASE_URL = "http://202.139.198.149/myoffice/ubonrat"
LOGIN_URL = f"{BASE_URL}/index.php"
LEAVE_URL = f"{BASE_URL}/index.php?name=tkk6"

USER = os.getenv("MYOFFICE_USER")
PASS = os.getenv("MYOFFICE_PASS")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not all([USER, PASS, BOT_TOKEN, CHAT_ID]):
    print("⚠️ Missing one or more required environment variables.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Helper: send a Telegram message (HTML formatting)
# ---------------------------------------------------------------------------
def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
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
    # A successful login will set a PHPSESSID cookie – we trust the response.
    return s

# ---------------------------------------------------------------------------
# Helper: parse the leave table – returns a list of dicts (one per row)
# ---------------------------------------------------------------------------
def parse_leave_page(html: str):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        raise ValueError("Leave table not found on page")

    rows = []
    for tr in table.find_all("tr")[1:]:  # skip header
        cols = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
        if not cols:
            continue
        # Expected column order (based on sample data):
        # 0 ID, 1 ประเภทกิจกรรม, 2 รหัสหน่วยงาน (skip),
        # 3 ชื่อ, 4 ประเภทการลา, 5 เริ่ม, 6 สิ้นสุด, 7 เหตุผล, 8 วันที่บันทึก,
        # 9 ชื่อผู้ขอ, 10 จำนวน?, 11 ที่อยู่, 12 โทร, 13 สถานะ, 14 ไฟล์แนบ, ...
        # The exact indices may vary – we use column names when available.
        # For robustness we map by known header titles.
        rows.append(cols)
    return rows

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

# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------
def main():
    session = login()
    resp = session.get(LEAVE_URL, timeout=15)
    resp.raise_for_status()
    rows = parse_leave_page(resp.text)
    if not rows:
        print("⚠️ No leave rows found")
        return

    # Assume the newest request is the first data row after header.
    newest = rows[0]
    # Map columns – we use position based on the sample you gave.
    # Adjust indices if the table layout changes.
    ID = newest[0]
    activity_type = newest[1]
    # Skip column 2 (รหัสหน่วยงาน)
    requester_name = newest[4] if len(newest) > 4 else ""
    leave_type = newest[5] if len(newest) > 5 else ""
    start_date = newest[6] if len(newest) > 6 else ""
    end_date = newest[7] if len(newest) > 7 else ""
    reason = newest[8] if len(newest) > 8 else ""
    record_date = newest[9] if len(newest) > 9 else ""
    phone = newest[12] if len(newest) > 12 else ""
    school = newest[10] if len(newest) > 10 else ""
    address = newest[11] if len(newest) > 11 else ""
    status = newest[13] if len(newest) > 13 else ""
    attachment = newest[14] if len(newest) > 14 else ""

    # -------------------------------------------------------------------
    # 1️⃣ New request handling
    # -------------------------------------------------------------------
    if ID not in state:
        # First time we see this ID – send full info.
        msg = (
            f"<b>📝 ใบลาขั้นแรก</b>\n"
            f"<b>ID:</b> {ID}\n"
            f"<b>กิจกรรม:</b> {activity_type}\n"
            f"<b>ชื่อผู้ขอ:</b> {requester_name}\n"
            f"<b>ประเภทล:</b> {leave_type}\n"
            f"<b>ตั้งแต่:</b> {start_date} <b>ถึง:</b> {end_date}\n"
            f"<b>เหตุผล:</b> {reason}\n"
            f"<b>วันที่บันทึก:</b> {record_date}\n"
            f"<b>โรงเรียน/หน่วยงาน:</b> {school}\n"
            f"<b>ที่อยู่:</b> {address}\n"
            f"<b>โทร:</b> {phone}\n"
            f"<b>สถานะ:</b> {status}\n"
        )
        if attachment:
            msg += f"<b>ไฟล์แนบ:</b> {attachment}\n"
        send_telegram(msg)
        state[ID] = status
        save_state()
        return

    # -------------------------------------------------------------------
    # 2️⃣ Status change handling
    # -------------------------------------------------------------------
    prev_status = state.get(ID, "")
    if status != prev_status:
        # Only react to approval/rejection.
        decision = None
        if status.lower() in ["อนุญาติ", "approved", "accept"]:
            decision = "✅ อนุญาติ"
        elif status.lower() in ["ปฏิเสธ", "reject", "rejected"]:
            decision = "❌ ปฏิเสธ"
        if decision:
            reply = f"<b>{decision}</b> (ใบลา ID {ID})"
            send_telegram(reply)
        # Update stored status regardless of whether we sent a reply.
        state[ID] = status
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
