import os, csv, requests, datetime as dt

LINE_TOKEN = os.environ["LINE_TOKEN"]
LINE_TO    = os.environ["LINE_TO"]
LOG_FILE   = "signal_log.csv"


def push_line(text):
    requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
        json={"to": LINE_TO, "messages": [{"type": "text", "text": text}]},
        timeout=10,
    )


def main():
    if not os.path.exists(LOG_FILE):
        push_line("📊 สรุปสัปดาห์: ยังไม่มี signal ที่ถูกส่งเลย")
        return

    week_ago = dt.datetime.utcnow() - dt.timedelta(days=7)
    rows = []
    with open(LOG_FILE) as f:
        for r in csv.DictReader(f):
            t = dt.datetime.strptime(r["time"], "%Y-%m-%d %H:%M")
            if t >= week_ago:
                rows.append(r)

    if not rows:
        push_line("📊 สรุปสัปดาห์: ไม่มี signal ในช่วง 7 วันที่ผ่านมา")
        return

    buy = sum(1 for r in rows if r["signal"] == "BUY")
    sell = sum(1 for r in rows if r["signal"] == "SELL")

    push_line(
        f"📊 สรุป XAU/USD 7 วันที่ผ่านมา\n"
        f"Signal ทั้งหมด: {len(rows)}\n"
        f"BUY: {buy} | SELL: {sell}\n"
        f"⚠️ นี่คือจำนวน signal ที่ส่ง ไม่ใช่ผลกำไร/ขาดทุนจริง\n"
        f"เช็คผลจริงจากบันทึกเทรดตัวเองเทียบ TP/SL ที่ให้ไป"
    )


if __name__ == "__main__":
    main()
