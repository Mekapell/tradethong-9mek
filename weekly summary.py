import os, csv, requests, datetime as dt

LINE_TOKEN = os.environ["LINE_TOKEN"]
LOG_FILE   = "signal_log.csv"


def push_to(user_id, text):
    requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
        json={"to": user_id, "messages": [{"type": "text", "text": text}]},
        timeout=10,
    )


def push_to_all_active(text):
    if not os.path.exists("users.json"):
        return
    import json
    users = json.load(open("users.json"))
    for uid, info in users.items():
        if not info.get("paused", False):
            push_to(uid, text)


def main():
    if not os.path.exists(LOG_FILE):
        push_to_all_active("📊 สรุปสัปดาห์: ยังไม่มี signal ที่ถูกส่งเลย")
        return

    week_ago = dt.datetime.utcnow() - dt.timedelta(days=7)
    rows = []
    with open(LOG_FILE) as f:
        for r in csv.DictReader(f):
            t = dt.datetime.strptime(r["time"], "%Y-%m-%d %H:%M")
            if t >= week_ago:
                rows.append(r)

    if not rows:
        push_to_all_active("📊 สรุปสัปดาห์: ไม่มี signal ในช่วง 7 วันที่ผ่านมา")
        return

    buy = sum(1 for r in rows if r["signal"] == "BUY")
    sell = sum(1 for r in rows if r["signal"] == "SELL")

    push_to_all_active(
        f"📊 สรุป XAU/USD 7 วันที่ผ่านมา\n"
        f"Signal ทั้งหมด: {len(rows)}\n"
        f"BUY: {buy} | SELL: {sell}\n"
        f"⚠️ นี่คือจำนวน signal ที่ส่ง ไม่ใช่ผลกำไร/ขาดทุนจริง"
    )


if __name__ == "__main__":
    main()
