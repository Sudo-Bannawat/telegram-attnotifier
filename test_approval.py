import os
import sys

from check_approval import (
    APPROVAL_URL,
    approval_message,
    fetch_popup,
    form_button,
    login,
    notify_new,
    parse_listing,
    require_env,
    save_state,
    state,
)


def main() -> None:
    require_env()
    session = login()
    resp = session.get(APPROVAL_URL, timeout=15)
    resp.raise_for_status()

    rows = parse_listing(resp.text)
    if not rows:
        print("❌ No approval records found on the page.")
        return

    rec = rows[0]
    detail = fetch_popup(rec["id"], session)
    heading = "🧪 [TEST BUILD] ขออนุมัติไปราชการ"

    # Reuse the rich-format helpers from check_approval.py instead of building
    # a separate HTML string in this test script.
    print("Preview message:")
    print(approval_message(rec, detail, heading))
    print("Preview keyboard:")
    print(form_button(rec["id"]))

    notify_new(rec, detail, heading)
    entry = state.get(rec["id"], {})
    telegram_message_id = entry.get("telegram_message_id")
    if telegram_message_id is None:
        print("❌ Failed to retrieve telegram_message_id from state.")
        return

    save_state()
    print(f"✅ Sent test approval message for ID {rec['id']}")
    print(f"📁 Stored telegram_message_id={telegram_message_id} in approval_state.json")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import traceback
        print("⚡️ Error:", exc)
        print("".join(traceback.format_exception(None, exc, exc.__traceback__)))
        sys.exit(1)
