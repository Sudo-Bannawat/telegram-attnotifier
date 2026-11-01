"""check_approval.py – Monitor MyOffice ขออนุมัติไปราชการ → Telegram notifications.

Mirrors check_leave.py design:
  * Polls approval_read&category=187 every 15 min (via GitHub Actions).
  * New request above watermark → full-detail Telegram message.
  * Status change → short reply in the same thread.
  * State saved to approval_state.json.

Secrets (same as check_leave.py):
  MYOFFICE_USER, MYOFFICE_PASS, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Optional:
  MYOFFICE_APPROVAL_CATEGORY   default "187"
  RESET_APPROVAL_STATE         "true" to re-announce all current pending items
"""

import json
import os
import re
import sys
import time
from html import escape
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_URL = "http://202.139.198.149/myoffice/ubonrat"
LOGIN_URL = f"{BASE_URL}/index.php?name=user&file=login"
APPROVAL_CATEGORY = os.getenv("MYOFFICE_APPROVAL_CATEGORY", "187")
APPROVAL_URL = (
    f"{BASE_URL}/index.php"
    f"?name=approval&op=approval_read&category={APPROVAL_CATEGORY}"
)
# Popup URL – confirmed from live page inspection
APPROVAL_POPUP_URL = (
    f"{BASE_URL}/popup2.php?name=approval&file=readapproval&id={{approval_id}}"
)

USER = os.getenv("MYOFFICE_USER")
PASS = os.getenv("MYOFFICE_PASS")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RESET_APPROVAL_STATE = os.getenv("RESET_APPROVAL_STATE") == "true"

if not all([USER, PASS, BOT_TOKEN, CHAT_ID]):
    print("⚠️  Missing one or more required environment variables.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def send_telegram(
    text: str,
    reply_to_message_id: int | None = None,
    reply_markup: dict | None = None,
) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload: dict = {
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
# Login
# ---------------------------------------------------------------------------
def login() -> requests.Session:
    s = requests.Session()
    resp = s.post(LOGIN_URL, data={"username": USER, "password": PASS}, timeout=15)
    resp.raise_for_status()
    sessid = s.cookies.get("PHPSESSID")
    if not sessid:
        raise ValueError("Login failed: no PHPSESSID cookie")
    if "ชื่อผู้ใช้ หรือ รหัสผ่าน ไม่ถูกต้อง" in resp.text:
        raise PermissionError("Login failed: wrong username/password")
    print(f"Logged in (PHPSESSID={sessid[:6]}…)")
    return s


# ---------------------------------------------------------------------------
# Parse listing page
#
# Live table columns (confirmed by browser inspection):
#   เรื่อง | เจ้าของเรื่อง | ผู้ผ่านแฟ้ม | สถานะ | การปฏิบัติ
#
# Each "เรื่อง" cell links to:
#   javascript:NewWindow('popup2.php?name=approval&file=readapproval&id=NNN',…)
# ---------------------------------------------------------------------------
_ID_RE = re.compile(r"[?&]id=(\d+)")


def parse_listing(html: str) -> list[dict]:
    """Return [{id, status, popup_url}] from the approval listing page."""
    if "ท่านไม่ใช่สมาชิก" in html:
        raise PermissionError("MyOffice session unauthenticated")

    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict] = []
    seen: set[str] = set()

    for tr in soup.find_all("tr"):
        # Find the popup link in this row
        popup_href = ""
        for a in tr.find_all("a"):
            href = a.get("href", "")
            if "readapproval" in href and "id=" in href:
                popup_href = href
                break

        m = _ID_RE.search(popup_href)
        if not m:
            continue
        aid = m.group(1)
        if aid in seen:
            continue
        seen.add(aid)

        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        # Row has 5 cells: เรื่อง | เจ้าของเรื่อง | ผู้ผ่านแฟ้ม | สถานะ | การปฏิบัติ
        # สถานะ is index 3 (0-based)
        status = cells[3].strip() if len(cells) > 3 else "รอดำเนินการ"
        rows.append({"id": aid, "status": status, "popup_href": popup_href})

    if not rows:
        text = soup.get_text(" ", strip=True)
        if "จำนวน 0" in text or "ไม่มีข้อมูล" in text:
            return []
        print("⚠️  No approval rows found on listing page.")
        return []

    print(f"  Listing: {len(rows)} approval row(s) found.")
    return rows


# ---------------------------------------------------------------------------
# Parse popup detail
#
# Live popup text (id=213) confirmed:
#   บันทึกข้อความ
#   ส่วนราชการ โรงเรียน…  วันที่ ๓o ตุลาคม ๒๕๖๙
#   เรื่อง ขออนุมัติไปราชการ
#   เรียน ผู้อำนวยการ…
#   ข้าพเจ้า {name} ตำแหน่ง {posit} สังกัด{school} พร้อมด้วย {companions}
#   ขออนุญาตไปราชการเพื่อ {purpose} ณ {destination}
#   ตั้งแต่วันที่ {start} ถึงวันที่ {end} มีกำหนด {days} วัน
#   โดยยานพาหนะ {vehicle}  ไม่ขอเบิก / ขอเบิก ค่าใช้จ่าย…
# ---------------------------------------------------------------------------
THAI_NUMS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")


def _cap(pattern: str, text: str, default: str = "") -> str:
    m = re.search(pattern, text, re.DOTALL)
    return m.group(1).strip() if m else default


def parse_popup(html: str, aid: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    text = " ".join(soup.stripped_strings).translate(THAI_NUMS)

    requester = _cap(r"ข้าพเจ้า\s+(.*?)\s+ตำแหน่ง", text)
    position = _cap(r"ตำแหน่ง\s+(.*?)\s+สังกัด", text)
    school = _cap(r"สังกัด(.*?)(?:พร้อมด้วย|ขออนุญาต)", text)
    companions = _cap(r"พร้อมด้วย\s+(.*?)\s+ขออนุญาต", text)
    purpose = _cap(r"ขออนุญาตไปราชการเพื่อ\s+(.*?)\s+(?:ณ\s|ตั้งแต่)", text)
    destination = _cap(r"ณ\s+(.*?)\s+ตั้งแต่วันที่", text)
    start_date = _cap(r"ตั้งแต่วันที่\s+(.*?)\s+ถึงวันที่", text)
    end_date = _cap(r"ถึงวันที่\s+(.*?)\s+มีกำหนด", text)
    days = _cap(r"มีกำหนด\s+(\d+)\s+วัน", text)
    vehicle = _cap(r"โดยยานพาหนะ\s+(.*?)\s+(?:และมีความประสงค์|ไม่ขอเบิก|ขอเบิก)", text)
    budget = "ขอเบิก" if "ขอเบิก" in text and "ไม่ขอเบิก" not in text else "ไม่ขอเบิก"
    record_date = _cap(r"วันที่\s+([\d๐-๙o]+\s+[ก-๙]+\s+[\d๐-๙]+)", text)

    return {
        "id": aid,
        "requester_name": requester,
        "position": position,
        "school": school.strip(),
        "companions": companions,
        "purpose": purpose,
        "destination": destination,
        "start_date": start_date,
        "end_date": end_date,
        "days": days,
        "vehicle": vehicle,
        "budget": budget,
        "record_date": record_date,
    }


def fetch_popup(aid: str, session: requests.Session) -> dict:
    url = APPROVAL_POPUP_URL.format(approval_id=aid)
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    return parse_popup(resp.text, aid)


# ---------------------------------------------------------------------------
# Status helpers (same words as check_leave.py)
# ---------------------------------------------------------------------------
_APPROVED = {"อนุมัติ", "อนุญาต", "อนุญาติ", "approved", "accept"}
_REJECTED = {"ปฏิเสธ", "ไม่อนุมัติ", "ไม่อนุญาต", "ไม่อนุญาติ", "reject", "rejected"}


def status_kind(status: str) -> str:
    v = status.strip().lower()
    if any(w in v for w in ("ยกเลิก", "cancel")):
        return "cancelled"
    if v in _APPROVED:
        return "approved"
    if v in _REJECTED:
        return "rejected"
    return "pending"


# ---------------------------------------------------------------------------
# Telegram Message Builders (Rich Formatting)
# ---------------------------------------------------------------------------
def approval_message(rec: dict, detail: dict, heading: str) -> str:
    # จัดการส่วนข้อมูลประกอบ (ผู้ร่วมเดินทาง)
    companions = detail.get("companions", "").strip()
    companions_txt = (
        f"\n👥 <b>ร่วมเดินทาง:</b> {escape(companions)}"
        if companions and companions != "-"
        else ""
    )

    # จัดการจำนวนวัน
    days_val = detail.get("days", "").strip()
    days_txt = f" <i>({escape(days_val)} วัน)</i>" if days_val else ""

    # จัดรูปแบบข้อความหลักสไตล์ Rich Document
    return (
        f"<b>{heading}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🔖 <b>เลขที่คำขอ:</b> <code>#{escape(rec['id'])}</code>\n"
        f"👤 <b>ผู้ยื่นคำขอ:</b> <b>{escape(detail['requester_name'])}</b>\n"
        f"💼 <b>ตำแหน่ง:</b> {escape(detail['position'])}"
        f"{companions_txt}\n\n"
        f"<b>📌 รายละเอียดการไปราชการ</b>\n"
        f"<blockquote>"
        f"📝 <b>วัตถุประสงค์:</b> {escape(detail['purpose'])}\n"
        f"📍 <b>สถานที่:</b> {escape(detail['destination'])}\n"
        f"📅 <b>ช่วงเวลา:</b> {escape(detail['start_date'])} — {escape(detail['end_date'])}{days_txt}\n"
        f"🚘 <b>พาหนะ:</b> {escape(detail['vehicle'])}\n"
        f"💳 <b>งบประมาณ:</b> {escape(detail['budget'])}\n"
        f"📊 <b>สถานะปัจจุบัน:</b> <b>{escape(rec['status'])}</b>"
        f"</blockquote>\n"
        f"🗓️ <i>ลงข้อมูลเมื่อ: {escape(detail['record_date'])}</i>"
    )


def approval_reply_message(aid: str, decision_title: str, status_text: str) -> str:
    """ข้อความตอบกลับ (Reply Thread) เมื่อมีการอัปเดตสถานะ เช่น อนุมัติ/ปฏิเสธ"""
    return (
        f"<b>{decision_title}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"<blockquote>"
        f"🔖 <b>คำขอไปราชการ:</b> <code>#{escape(aid)}</code>\n"
        f"📌 <b>สถานะใหม่:</b> <b>{escape(status_text)}</b>"
        f"</blockquote>"
    )


def form_button(aid: str) -> dict:
    """ปุ่ม Inline Keyboard กดเปิดดูเอกสารจริงใน MyOffice"""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🔗 เปิดเอกสารใน MyOffice",
                    "url": APPROVAL_POPUP_URL.format(approval_id=aid),
                }
            ]
        ]
    }


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------
STATE_FILE = Path(__file__).with_name("approval_state.json")
state: dict = {}
if STATE_FILE.exists():
    content = STATE_FILE.read_text(encoding="utf-8-sig").strip()
    state = json.loads(content) if content else {}


def save_state() -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def state_entry(aid: str) -> dict:
    entry = state.get(aid, {})
    return {"status": entry} if isinstance(entry, str) else entry


def approval_num(rec: dict) -> int:
    try:
        return int(rec["id"])
    except (TypeError, ValueError):
        return 0


def known_watermark() -> int:
    ids = [int(k) for k in state if k.isdigit()]
    return max([int(state.get("_watermark", 0)), *ids], default=0)


def notify_new(rec: dict, detail: dict, heading: str) -> None:
    result = send_telegram(
        approval_message(rec, detail, heading),
        reply_markup=form_button(rec["id"]),
    )
    state[rec["id"]] = {
        "status": rec["status"],
        "telegram_message_id": result["result"]["message_id"],
    }
    save_state()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    global state
    session = login()

    resp = session.get(APPROVAL_URL, timeout=15)
    resp.raise_for_status()
    rows = parse_listing(resp.text)

    if RESET_APPROVAL_STATE:
        state = {"_watermark": max((approval_num(r) for r in rows), default=0)}
        save_state()
        for rec in sorted(rows, key=approval_num):
            detail = fetch_popup(rec["id"], session)
            notify_new(rec, detail, "✈️ ขออนุมัติไปราชการ (ระบบรีเซ็ต)")
            time.sleep(1.5)
        return

    rows.sort(key=approval_num)
    watermark = known_watermark()

    # 1) New requests above watermark
    new_rows = (
        [r for r in rows if r["id"] not in state and approval_num(r) > watermark]
        if watermark
        else rows[-1:]
    )
    for rec in new_rows:
        detail = fetch_popup(rec["id"], session)
        notify_new(rec, detail, "✈️ มีคำขออนุมัติไปราชการใหม่")
        state["_watermark"] = max(int(state.get("_watermark", 0)), approval_num(rec))
        save_state()
        time.sleep(1.5)

    # 2) Status changes on already-tracked requests
    for rec in rows:
        aid = rec["id"]
        if aid not in state:
            continue
        entry = state_entry(aid)
        if rec["status"] == entry.get("status", ""):
            continue
        kind = status_kind(rec["status"])
        if kind == "approved":
            decision = "✅ อนุมัติ"
            status_text = "อนุมัติ"
        elif kind == "rejected":
            decision = "❌ ปฏิเสธ"
            status_text = "ปฏิเสธ"
        else:
            decision = None
            status_text = rec["status"]
        if decision:
            reply = approval_reply_message(aid, decision, status_text)
            send_telegram(reply, entry.get("telegram_message_id"))
            time.sleep(1.5)
        state[aid] = {**entry, "status": rec["status"]}
        save_state()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import traceback
        print("⚡️ Error:", exc)
        print("".join(traceback.format_exception(None, exc, exc.__traceback__)))
        sys.exit(1)
