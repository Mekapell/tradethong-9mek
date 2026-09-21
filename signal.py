import os, requests, datetime as dt

LINE_TOKEN   = os.environ["LINE_TOKEN"]
LINE_TO      = os.environ["LINE_TO"]
TWELVE_KEY   = os.environ["TWELVEDATA_KEY"]
FINNHUB_KEY  = os.environ["FINNHUB_KEY"]
STATE_FILE   = "last_signal.txt"

NEG_WORDS = ["war", "conflict", "crisis", "rate hike", "inflation surge", "recession", "sanction"]
POS_WORDS = ["rate cut", "stimulus", "safe haven", "dovish", "ceasefire", "easing"]


def push_line(text):
    requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
        json={"to": LINE_TO, "messages": [{"type": "text", "text": text}]},
        timeout=10,
    )


def get_series(interval, size=60):
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": "XAU/USD", "interval": interval, "outputsize": size, "apikey": TWELVE_KEY},
        timeout=10,
    ).json()
    closes = [float(v["close"]) for v in reversed(r["values"])]
    return closes


def sma(vals, n):
    return sum(vals[-n:]) / n


def tech_signal(interval):
    c = get_series(interval, 60)
    fast, slow = sma(c, 10), sma(c, 30)
    if fast > slow:
        return "BUY", c[-1]
    if fast < slow:
        return "SELL", c[-1]
    return "WAIT", c[-1]


def news_signal():
    news = requests.get(
        "https://finnhub.io/api/v1/news",
        params={"category": "forex", "token": FINNHUB_KEY},
        timeout=10,
    ).json()
    text = " ".join((n.get("headline", "") + n.get("summary", "")).lower() for n in news[:30])
    score = sum(w in text for w in POS_WORDS) - sum(w in text for w in NEG_WORDS)
    if score > 0:
        return "BUY"
    if score < 0:
        return "SELL"
    return "WAIT"


def combine(tf_signals, news):
    votes = list(tf_signals.values()) + [news]
    if votes.count("BUY") >= 3:
        return "BUY"
    if votes.count("SELL") >= 3:
        return "SELL"
    return "WAIT"


def load_last():
    if os.path.exists(STATE_FILE):
        return open(STATE_FILE).read().strip()
    return ""


def save_last(sig):
    open(STATE_FILE, "w").write(sig)


def main():
    tfs = {"15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h", "1d": "1day"}
    tf_signals, last_price = {}, 0
    for label, interval in tfs.items():
        sig, price = tech_signal(interval)
        tf_signals[label] = sig
        last_price = price

    news = news_signal()
    final = combine(tf_signals, news)
    last = load_last()

    if final != last:
        now = dt.datetime.utcnow()
        lines = " | ".join(f"{k}:{v}" for k, v in tf_signals.items())
        push_line(
            f"🟡 XAU/USD | {final}\n"
            f"ราคา: {last_price:.2f}\n"
            f"Tech: {lines}\n"
            f"News: {news}\n"
            f"เวลา: {now:%Y-%m-%d %H:%M} UTC"
        )
        save_last(final)


if __name__ == "__main__":
    main()
