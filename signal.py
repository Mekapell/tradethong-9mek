import os, json, requests, datetime as dt

LINE_TOKEN      = os.environ["LINE_TOKEN"]
LINE_TO         = os.environ["LINE_TO"]
TWELVE_KEY      = os.environ["TWELVEDATA_KEY"]
FINNHUB_KEY     = os.environ["FINNHUB_KEY"]
ACCOUNT_BALANCE = float(os.environ.get("ACCOUNT_BALANCE", "1000"))
RISK_PERCENT    = float(os.environ.get("RISK_PERCENT", "1"))
STATE_FILE      = "last_signal.txt"
LOG_FILE        = "signal_log.csv"
NEWS_CACHE      = "news_cache.json"
DXY_CACHE       = "dxy_cache.json"
CACHE_MINUTES   = 60
SHARP_THRESHOLD = 15  # ขนาดการกลับตัวขั้นต่ำ (0-100 scale) ถึงจะนับว่า "แหลม"

NEG_WORDS = ["war", "conflict", "crisis", "rate hike", "inflation surge", "recession",
             "sanction", "hawkish", "strong dollar", "yields rise", "geopolitical tension"]
POS_WORDS = ["rate cut", "stimulus", "safe haven", "dovish", "ceasefire", "easing",
             "weak dollar", "yields fall", "fed pause", "de-escalation"]


def push_line(text):
    requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
        json={"to": LINE_TO, "messages": [{"type": "text", "text": text}]},
        timeout=10,
    )


def get_series(interval, size=100):
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": "XAU/USD", "interval": interval, "outputsize": size, "apikey": TWELVE_KEY},
        timeout=10,
    ).json()
    vals = list(reversed(r["values"]))
    closes = [float(v["close"]) for v in vals]
    highs = [float(v["high"]) for v in vals]
    lows = [float(v["low"]) for v in vals]
    return closes, highs, lows


def sma(vals, n):
    return [sum(vals[i - n + 1:i + 1]) / n for i in range(n - 1, len(vals))]


def stochastic(closes, highs, lows, period=5, smooth_k=3, smooth_d=3):
    raw_k = []
    for i in range(period - 1, len(closes)):
        window_high = max(highs[i - period + 1:i + 1])
        window_low = min(lows[i - period + 1:i + 1])
        if window_high == window_low:
            raw_k.append(50)
        else:
            raw_k.append((closes[i] - window_low) / (window_high - window_low) * 100)
    slow_k = sma(raw_k, smooth_k)
    d = sma(slow_k, smooth_d)
    # ตัด slow_k ให้ยาวเท่า d เพื่อจับคู่ index ท้ายๆตรงกัน
    slow_k = slow_k[-len(d):]
    return slow_k, d


def detect_turn(k, d):
    if len(k) < 3 or len(d) < 2:
        return "WAIT"

    diff1 = k[-2] - k[-3]
    diff2 = k[-1] - k[-2]

    # จุดหักแหลมจริง (V-shape แรงพอทั้ง 2 ข้าง)
    if diff1 < 0 and diff2 > 0 and abs(diff1) >= SHARP_THRESHOLD and abs(diff2) >= SHARP_THRESHOLD:
        return "BUY_CONFIRMED" if d[-1] > d[-2] else "BUY_STARTING"
    if diff1 > 0 and diff2 < 0 and abs(diff1) >= SHARP_THRESHOLD and abs(diff2) >= SHARP_THRESHOLD:
        return "SELL_CONFIRMED" if d[-1] < d[-2] else "SELL_STARTING"

    # เริ่มหัก แต่ยังไม่แหลมพอ
    if diff1 <= 0 and diff2 > 0:
        return "BUY_STARTING"
    if diff1 >= 0 and diff2 < 0:
        return "SELL_STARTING"

    return "WAIT"


def atr(highs, lows, closes, n=14):
    trs = []
    for i in range(1, n + 1):
        tr = max(highs[-i] - lows[-i], abs(highs[-i] - closes[-i - 1]), abs(lows[-i] - closes[-i - 1]))
        trs.append(tr)
    return sum(trs) / n


def tf_signal(interval):
    closes, highs, lows = get_series(interval, 100)
    k, d = stochastic(closes, highs, lows)
    turn = detect_turn(k, d)
    return turn, closes[-1], atr(highs, lows, closes), k[-1]


def load_cache(path):
    if not os.path.exists(path):
        return None
    data = json.load(open(path))
    ts = dt.datetime.fromisoformat(data["ts"])
    if (dt.datetime.utcnow() - ts).total_seconds() < CACHE_MINUTES * 60:
        return data["value"]
    return None


def save_cache(path, value):
    json.dump({"ts": dt.datetime.utcnow().isoformat(), "value": value}, open(path, "w"))


def news_signal():
    cached = load_cache(NEWS_CACHE)
    if cached is not None:
        return cached
    news = requests.get(
        "https://finnhub.io/api/v1/news",
        params={"category": "forex", "token": FINNHUB_KEY},
        timeout=10,
    ).json()
    text = " ".join((n.get("headline", "") + n.get("summary", "")).lower() for n in news[:40])
    score = sum(w in text for w in POS_WORDS) - sum(w in text for w in NEG_WORDS)
    result = "BUY" if score >= 2 else "SELL" if score <= -2 else "WAIT"
    save_cache(NEWS_CACHE, result)
    return result


def dxy_direction():
    cached = load_cache(DXY_CACHE)
    if cached is not None:
        return cached if cached != "NONE" else None
    try:
        r = requests.get(
            "https://api.twelvedata.com/time_series",
            params={"symbol": "DXY", "interval": "1h", "outputsize": 10, "apikey": TWELVE_KEY},
            timeout=10,
        ).json()
        closes = [float(v["close"]) for v in reversed(r["values"])]
        result = "UP" if closes[-1] > closes[0] else "DOWN"
        save_cache(DXY_CACHE, result)
        return result
    except Exception:
        save_cache(DXY_CACHE, "NONE")
        return None


def is_paused():
    return os.path.exists("paused.txt") and open("paused.txt").read().strip() == "true"


def is_low_liquidity(now):
    return now.hour < 7


def calc_lot(entry, sl):
    risk_dollar = ACCOUNT_BALANCE * (RISK_PERCENT / 100)
    distance = abs(entry - sl)
    if distance == 0:
        return 0
    return round(risk_dollar / (distance * 100), 2)


def log_signal(now, final, price, tp, sl, lot):
    is_new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a") as f:
        if is_new:
            f.write("time,signal,price,tp,sl,lot\n")
        f.write(f"{now:%Y-%m-%d %H:%M},{final},{price:.2f},{tp:.2f},{sl:.2f},{lot}\n")


def load_last():
    if os.path.exists(STATE_FILE):
        return open(STATE_FILE).read().strip()
    return ""


def save_last(sig):
    open(STATE_FILE, "w").write(sig)


def main():
    if is_paused():
        return

    now = dt.datetime.utcnow()
    if is_low_liquidity(now):
        return

    sig15, price, a, k15 = tf_signal("15min")
    sig30, _, _, k30 = tf_signal("30min")
    sig1h, _, _, k1h = tf_signal("1h")

    final = "WAIT"
    if sig15 == "BUY_CONFIRMED" and sig30 in ("BUY_CONFIRMED", "BUY_STARTING"):
        final = "BUY"
    elif sig15 == "SELL_CONFIRMED" and sig30 in ("SELL_CONFIRMED", "SELL_STARTING"):
        final = "SELL"

    # 1h ใช้เตือนถ้าสวนทางรุนแรง ไม่บังคับบล็อก แค่ลดความมั่นใจ
    warn_1h = ""
    if final == "BUY" and sig1h == "SELL_CONFIRMED":
        warn_1h = "⚠️ 1h กำลังหักลงสวนทาง ระวัง"
    elif final == "SELL" and sig1h == "BUY_CONFIRMED":
        warn_1h = "⚠️ 1h กำลังหักขึ้นสวนทาง ระวัง"

    dxy = dxy_direction()
    if dxy:
        if final == "BUY" and dxy == "UP":
            final = "WAIT"
        elif final == "SELL" and dxy == "DOWN":
            final = "WAIT"

    news = news_signal()
    last = load_last()

    if final != last:
        tp_sl, lot = "", 0
        if final == "BUY":
            tp, sl = price + a * 2, price - a * 1.5
            lot = calc_lot(price, sl)
            tp_sl = f"TP: {tp:.2f} | SL: {sl:.2f} | Lot: {lot} (risk {RISK_PERCENT}% ของ ${ACCOUNT_BALANCE:.0f})"
        elif final == "SELL":
            tp, sl = price - a * 2, price + a * 1.5
            lot = calc_lot(price, sl)
            tp_sl = f"TP: {tp:.2f} | SL: {sl:.2f} | Lot: {lot} (risk {RISK_PERCENT}% ของ ${ACCOUNT_BALANCE:.0f})"

        push_line(
            f"🟡 XAU/USD | {final}\n"
            f"ราคา: {price:.2f}\n"
            f"{tp_sl}\n"
            f"Stoch K: 15m={k15:.1f} 30m={k30:.1f} 1h={k1h:.1f}\n"
            f"News: {news}\n"
            f"{warn_1h}\n"
            f"เวลา: {now:%Y-%m-%d %H:%M} UTC\n"
            f"⚠️ ไม่การันตีผล ใช้ควบคู่การจัดการความเสี่ยงเอง"
        )
        save_last(final)
        if final in ("BUY", "SELL"):
            log_signal(now, final, price, tp, sl, lot)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        push_line(f"🛑 SYSTEM ERROR\n{type(e).__name__}: {e}\nเวลา: {dt.datetime.utcnow():%Y-%m-%d %H:%M} UTC")
        raise
