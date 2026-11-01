import json
import os
import re
import sys
from html import escape

import requests
from bs4 import BeautifulSoup

BASE_URL = "http://202.139.198.149/myoffice/ubonrat"
LOGIN_URL = f"{BASE_URL}/index.php?name=user&file=login"
APPROVAL_CATEGORY = os.getenv("MYOFFICE_APPROVAL_CATEGORY", "187")
APPROVAL_URL = (
    f"{BASE_URL}/index.php?name=approval&op=approval_read&category={APPROVAL_CATEGORY}"
)

USER = os.getenv("MYOFFICE_USER")
PASS = os.getenv("MYOFFICE_PASS")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
STATE_FILE = "approval_state.json"

if not all([USER, PASS, BOT_TOKEN, CHAT_ID]):
    print("⚠️ Missing environment variables.")
    sys.exit(1)


def send_telegram(text: str) -> dict:
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


def login() -> requests.Session:
    s = requests.Session()
    resp = s.post(LOGIN_URL, data={"username": USER, "password": PASS}, timeout=15)
    resp.raise_for_status()
    return s


def parse_first_approval(html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    id_re = re.compile(r"[?&]id=(\d+)")

    for tr in soup.find_all("tr"):
        for a in tr.find_all("a"):
            href = a.get("href", "")
            if "readapproval" in href and "id=" in href:
                match = id_re.search(href)
                if not match:
                    continue

                app_id = match.group(1)
                cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]

                topic = cells[0] if cells else "ขออนุมัติไปราชการ"
                status = cells[3] if len(cells) > 3 else "เสนอ"
                return {"id": app_id, "topic": topic, "status": status}
    return None


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def main():
    session = login()
    resp = session.get(APPROVAL_URL, timeout=15)
    resp.raise_for_status()

    item = parse_first_approval(resp.text)
    if not item:
        print("❌ No approval records found on the page.")
        return

    app_id = item["id"]
    topic = escape(item["topic"])
    status = escape(item["status"])

    msg_text = (
        f"<b>🧪 [TEST BUILD] รายการขออนุมัติไปราชการ</b>\n"
        f"<b>เรื่อง:</b> {topic}\n"
        f"<b>สถานะปัจจุบัน:</b> {status}\n"
        f"<b>ID:</b> <code>{app_id}</code>\n\n"
        f"<i>ข้อความนี้ส่งเพื่อสร้างและบันทึก message_id สำหรับ Reply</i>"
    )

    res = send_telegram(msg_text)
    message_id = res.get("result", {}).get("message_id")
    if not message_id:
        print("❌ Failed to retrieve message_id from Telegram response.")
        return

    state = load_state()
    state[app_id] = {
        "status": status,
        "topic": item["topic"],
        "message_id": message_id,
        "telegram_message_id": message_id,
        "reply_parameters": {"message_id": message_id},
    }

    save_state(state)

    print(f"✅ Sent test message for approval ID {app_id}")
    print(f"📁 Updated {STATE_FILE} with message_id={message_id}")


if __name__ == "__main__":
    main()
