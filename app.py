import os, time, json, logging, random, threading
from datetime import datetime
from threading import Thread
from flask import Flask, jsonify
import requests
import feedparser, pytz

# ---------- Config & Modes ----------
DEFAULT_TEST_MODE = os.getenv("TEST_MODE", "true").lower() == "true"
SIMULATION = os.getenv("SIMULATION", "true").lower() == "true"
AUTO_MODE_ENV = os.getenv("AUTO_MODE", "false").lower() == "true"
EXCHANGE_ID = os.getenv("EXCHANGE", "upbit")
API_KEY = os.getenv("API_KEY", "")
API_SECRET = os.getenv("API_SECRET", "")
SYMBOL = os.getenv("SYMBOL", "BTC/KRW")
TOTAL_KRW = float(os.getenv("TOTAL_KRW", 200000))
N_GRIDS = int(os.getenv("N_GRIDS", 20))
PRICE_LOW = os.getenv("PRICE_LOW")
PRICE_HIGH = os.getenv("PRICE_HIGH")
GRID_MODE = os.getenv("GRID_MODE", "equal")
PRICE_PADDING = float(os.getenv("PRICE_PADDING", 0.0))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 5))
CONFIRM_TIMEOUT = int(os.getenv("CONFIRM_TIMEOUT", 30))
DATA_FILE = os.getenv("DATA_FILE", "grid_state.json")
LOGFILE = os.getenv("LOGFILE", "grid_trader.log")
PORT = int(os.getenv("PORT", 8080))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""

# --- 뉴스 환경 ---
NEWS_ENABLED_DEFAULT = os.getenv("NEWS_ENABLED", "true").lower() == "true"
NEWS_INTERVAL_MIN = int(os.getenv("NEWS_INTERVAL_MIN", 60))
NEWS_MAX_ITEMS = int(os.getenv("NEWS_MAX_ITEMS", 5))
NEWS_SOURCES = [s.strip().lower() for s in os.getenv("NEWS_SOURCES", "coindesk,cointelegraph").split(",")]
NEWS_FILTER = [s.strip().lower() for s in os.getenv("NEWS_FILTER", "bitcoin,btc").split(",") if s.strip()]
LOCAL_TZ = pytz.timezone(os.getenv("TIMEZONE", "Asia/Seoul"))

RSS_MAP = {
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "cointelegraph": "https://cointelegraph.com/rss",
    "bitcoinmagazine": "https://bitcoinmagazine.com/.rss/full/",
}

# ---------- Logging ----------
logger = logging.getLogger("grid_trader")
logger.setLevel(logging.INFO)
fh = logging.FileHandler(LOGFILE)
fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
fh.setFormatter(fmt)
logger.addHandler(fh)
logger.addHandler(logging.StreamHandler())

# ---------- State ----------
state_lock = threading.Lock()
telegram_answers = {}

def load_state():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "krw": TOTAL_KRW, "btc": 0.0,
        "grid_orders": {},
        "auto_mode": AUTO_MODE_ENV,
        "test_mode": DEFAULT_TEST_MODE,
        "news_enabled": NEWS_ENABLED_DEFAULT,
        "news_filter": NEWS_FILTER,
        "news_seen_ids": [],
        "strategy": None,
        "price_low": None, "price_high": None,
        "n_grids": N_GRIDS, "price_padding": PRICE_PADDING, "check_interval": CHECK_INTERVAL,
        "updated_at": datetime.utcnow().isoformat()
    }

def save_state(s):
    s["updated_at"] = datetime.utcnow().isoformat()
    with open(DATA_FILE, "w") as f:
        json.dump(s, f, indent=2, default=str)

# ---------- Price feeds ----------
class LivePriceFeed:
    def __init__(self):
        import ccxt
        ex_class = getattr(ccxt, EXCHANGE_ID)
        cfg = {"apiKey": API_KEY, "secret": API_SECRET, "enableRateLimit": True}
        proxy = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
        if proxy:
            cfg["proxies"] = {"http": proxy, "https": proxy}
        self.ex = ex_class(cfg)
    def last(self, symbol):
        t = self.ex.fetch_ticker(symbol)
        return float(t["last"])

class TestPriceFeed:
    def __init__(self, start_price=None, vol=None):
        self.price = start_price or float(os.getenv("TEST_START_PRICE", 70_000_000))
        self.vol = vol or float(os.getenv("TEST_VOL", 0.002))
        random.seed(42)
    def last(self, symbol):
        step = random.uniform(-self.vol, self.vol)
        self.price *= (1 + step)
        return round(self.price, 0)

try:
    live_feed = LivePriceFeed()
except Exception as e:
    live_feed = None
    logger.warning(f"LivePriceFeed init failed (ok in TEST_MODE): {e}")
test_feed = TestPriceFeed()

def get_price(symbol):
    with state_lock:
        s = load_state()
        use_test = s.get("test_mode", True)
    if use_test or live_feed is None:
        return test_feed.last(symbol)
    return live_feed.last(symbol)

# ---------- Utils ----------
def frange(start, stop, n):
    if n <= 1:
        return [start]
    step = (stop - start) / float(n - 1)
    return [start + i * step for i in range(n)]

def build_grid(price_low, price_high, n_grids, mode='equal'):
    if mode == 'equal':
        return frange(price_low, price_high, n_grids + 1)
    ratios = [i / n_grids for i in range(n_grids + 1)]
    return [price_low * (price_high / price_low) ** r for r in ratios]

# --- [VALIDATION] Tick/Min rules for Upbit ------------------------------------
def krw_tick_size(price: float) -> float:
    p = float(price)
    if p >= 2_000_000: return 1000
    if p >= 1_000_000: return 1000
    if p >=   500_000: return 500
    if p >=   100_000: return 100
    if p >=    50_000: return 50
    if p >=    10_000: return 10
    if p >=     5_000: return 5
    if p >=     1_000: return 1
    if p >=       100: return 1
    if p >=        10: return 0.1
    if p >=         1: return 0.01
    if p >=       0.1: return 0.001
    if p >=      0.01: return 0.0001
    if p >=     0.001: return 0.00001
    if p >=    0.0001: return 0.000001
    if p >=   0.00001: return 0.0000001
    return 0.00000001

def normalize_to_tick(value: float, tick: float) -> float:
    if tick <= 0:
        return float(value)
    return round(round(float(value) / tick) * tick, 8)

def normalize_decimals(x: float, precision_decimals: int or None) -> float:
    if precision_decimals is None:
        return float(x)
    q = 10 ** precision_decimals
    return round(float(x) * q) / q

def get_ccxt_specs(symbol: str):
    try:
        import ccxt
        ex_class = getattr(ccxt, EXCHANGE_ID)
        cfg = {"apiKey": API_KEY, "secret": API_SECRET, "enableRateLimit": True}
        proxy = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
        if proxy:
            cfg["proxies"] = {"http": proxy, "https": proxy}
        ex = ex_class(cfg)
        markets = ex.load_markets()
        m = markets.get(symbol)
        if not m:
            return None
        price_prec = None
        amt_prec = None
        if isinstance(m.get("precision"), dict):
            price_prec = m["precision"].get("price")
            amt_prec = m["precision"].get("amount")
        limits = (m.get("limits") or {})
        min_cost = (limits.get("cost") or {}).get("min")
        min_amt  = (limits.get("amount") or {}).get("min")
        return {"price_prec": price_prec, "amt_prec": amt_prec, "min_cost": min_cost, "min_amt": min_amt}
    except Exception:
        return None

def validate_order(symbol: str, side: str, price: float, amount: float):
    s = symbol.upper()
    px = float(price)
    qty = float(amount)
    specs = get_ccxt_specs(symbol)
    price_prec = specs.get("price_prec") if specs else None
    amt_prec   = specs.get("amt_prec") if specs else None
    min_cost   = specs.get("min_cost") if specs else None
    min_amt    = specs.get("min_amt") if specs else None

    if s.endswith("/KRW"):
        tick = krw_tick_size(px)
        px = normalize_to_tick(px, tick)
        min_total = 5000.0
        if isinstance(min_cost, (int, float)) and min_cost > 0:
            min_total = max(min_total, float(min_cost))
        qty = normalize_decimals(qty, amt_prec)
        total = px * qty
        if total + 1e-9 < min_total:
            need = min_total / max(px, 1e-12)
            return (False, f"KRW 최소주문금액 {int(min_total):,}원 미만 (현재 {int(total):,}원). 수량≥{need:.8f} 필요", px, qty)

    elif s.endswith("/USDT"):
        if price_prec is not None:
            px = normalize_decimals(px, price_prec)
        else:
            if px >= 1: tick = 0.01
            elif px >= 0.1: tick = 0.001
            else: tick = 0.0001
            px = normalize_to_tick(px, tick)
        min_total = 0.5
        if isinstance(min_cost, (int, float)) and min_cost > 0:
            min_total = max(min_total, float(min_cost))
        qty = normalize_decimals(qty, amt_prec)
        total = px * qty
        if total + 1e-12 < min_total:
            need = min_total / max(px, 1e-12)
            return (False, f"USDT 최소주문금액 {min_total} USDT 미만 (현재 {total:.6f}). 수량≥{need:.8f} 필요", px, qty)

    elif s.endswith("/BTC"):
        min_q = 0.00005
        if isinstance(min_amt, (int, float)) and min_amt > 0:
            min_q = max(min_q, float(min_amt))
        qty = normalize_decimals(qty, amt_prec)
        if qty + 1e-12 < min_q:
            return (False, f"BTC 마켓 최소 주문수량 {min_q} BTC 미만 (현재 {qty})", px, qty)
        px = normalize_decimals(px, price_prec)

    else:
        px = normalize_decimals(px, price_prec)
        qty = normalize_decimals(qty, amt_prec)
        if isinstance(min_cost, (int, float)) and min_cost > 0 and px * qty + 1e-12 < float(min_cost):
            need = float(min_cost) / max(px, 1e-12)
            return (False, f"최소 주문 금액 {min_cost} 미만. 수량≥{need:.8f} 필요", px, qty)
        if isinstance(min_amt, (int, float)) and min_amt > 0 and qty + 1e-12 < float(min_amt):
            return (False, f"최소 주문 수량 {min_amt} 미만 (현재 {qty})", px, qty)

    return (True, "OK", px, qty)
# --- [VALIDATION] END ----------------------------------------------------------

def tg_send(text):
    if not TELEGRAM_API or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(f"{TELEGRAM_API}/sendMessage", data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")

def tg_send_confirm(text, payload_id):
    if not TELEGRAM_API or not TELEGRAM_CHAT_ID:
        return False
    keyboard = {
        "inline_keyboard": [[
            {"text": "예", "callback_data": json.dumps({"id": payload_id, "ans": "yes"})},
            {"text": "아니오", "callback_data": json.dumps({"id": payload_id, "ans": "no"})}
        ]]
    }
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "reply_markup": json.dumps(keyboard)}
    try:
        r = requests.post(f"{TELEGRAM_API}/sendMessage", data=data, timeout=10)
        return r.ok
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")
        return False

# ---------- Strategy Presets ----------
STRATEGY_PROFILES = {
    "up": {"name":"Up (상승장)","up_pct":0.025,"down_pct":0.010,"n_grids":30,"padding":0.0,"interval":3,"target_note":"다음 그리드 도달 시 매도"},
    "middle":{"name":"Middle (횡보장)","up_pct":0.015,"down_pct":0.015,"n_grids":40,"padding":0.0,"interval":3,"target_note":"다음 그리드 도달 시 매도"},
    "down":{"name":"Down (하락장)","up_pct":0.008,"down_pct":0.030,"n_grids":20,"padding":0.0,"interval":5,"target_note":"반등 시 빠른 매도"},
}

def apply_strategy_profile(s, current_price, key):
    prof = STRATEGY_PROFILES.get(key)
    if not prof:
        return None
    low = current_price * (1.0 - prof["down_pct"])
    high = current_price * (1.0 + prof["up_pct"])
    s["strategy"] = key
    s["price_low"] = low
    s["price_high"] = high
    s["n_grids"] = prof["n_grids"]
    s["price_padding"] = prof["padding"]
    s["check_interval"] = prof["interval"]
    return (f"전략: {prof['name']} ({key})\n"
            f"범위: {int(low):,} ~ {int(high):,}\n"
            f"N_GRIDS: {prof['n_grids']} | PADDING: {prof['padding']} | INTERVAL: {prof['interval']}s\n"
            f"목표가: {prof['target_note']}")

# ---------- Orders ----------
def place_order(side, price, amount):
    ok, msg, adj_price, adj_amount = validate_order(SYMBOL, side, price, amount)
    if not ok:
        logger.info(f"[ORDER REJECT] {msg}")
        tg_send(f"❌ 주문 거절: {msg}")
        return None
    price = adj_price
    amount = adj_amount
    slippage = 0.003
    exec_price = price * (1 + slippage if side == "sell" else 1 - slippage)

    with state_lock:
        s = load_state()
        if side == "buy":
            cost = exec_price * amount
            if s["krw"] >= cost:
                s["krw"] -= cost
                s["btc"] += amount
            else:
                logger.info("KRW 부족 → 매수 불가")
                return None
        else:
            if s["btc"] >= amount:
                s["btc"] -= amount
                s["krw"] += exec_price * amount
            else:
                logger.info("BTC 부족 → 매도 불가")
                return None
        save_state(s)

    logger.info(f"[SIM] {side.upper()} {amount} {SYMBOL} @ {int(exec_price):,}")
    with state_lock:
        s2 = load_state()
    if s2.get("auto_mode") and TELEGRAM_API and TELEGRAM_CHAT_ID:
        tg_send(f"[AUTO 체결] {side.upper()} {amount} {SYMBOL} @ {int(exec_price):,}\nKRW: {int(s2['krw']):,} / BTC: {s2['btc']}")
    return {"id": f"SIM-{side}-{int(time.time())}", "side": side, "price": exec_price, "amount": amount, "status": "closed"}

# ---------- Strategy tick ----------
def run_grid_once():
    with state_lock:
        s = load_state()

    current = get_price(SYMBOL)

    low = s.get("price_low") or (float(PRICE_LOW) if PRICE_LOW else current * 0.98)
    high = s.get("price_high") or (float(PRICE_HIGH) if PRICE_HIGH else current * 1.02)
    ng = s.get("n_grids", N_GRIDS)
    pad = s.get("price_padding", PRICE_PADDING)

    if low >= high:
        logger.warning("PRICE_LOW < PRICE_HIGH 이어야 합니다")
        return

    levels = build_grid(low, high, ng, GRID_MODE)
    go = s.get("grid_orders", {})
    order_krw = TOTAL_KRW / ng

    for i in range(ng):
        buy_price = levels[i] + pad
        sell_price = levels[i + 1] - pad
        amount = round(order_krw / max(buy_price, 1), 8)
        key = str(i)
        if key not in go:
            go[key] = {"buy_price": buy_price, "sell_price": sell_price, "amount": amount, "status": "idle"}

    for k, g in go.items():
        if g["status"] == "idle" and current <= g["buy_price"]:
            with state_lock:
                s2 = load_state()
                need_confirm = (not s2.get("auto_mode", False))
            do_place = True
            if need_confirm and TELEGRAM_API and TELEGRAM_CHAT_ID:
                pid = f"buy_{k}_{int(time.time())}"
                tg_send_confirm(f"그리드 #{k} 매수 승인?\n코인: {SYMBOL}\n매수가: {int(g['buy_price']):,}\n수량: {g['amount']}\n(응답 {CONFIRM_TIMEOUT}s)", pid)
                waited = 0
                ans = None
                while waited < CONFIRM_TIMEOUT:
                    if pid in telegram_answers:
                        ans = telegram_answers.pop(pid)[0]
                        break
                    time.sleep(1)
                    waited += 1
                if ans != "yes":
                    do_place = False
            if do_place:
                order = place_order("buy", g["buy_price"], g["amount"])
                if order:
                    go[k]["status"] = "bought"
                    go[k]["buy_order"] = order

        if g["status"] == "bought" and current >= g["sell_price"]:
            order = place_order("sell", g["sell_price"], g["amount"])
            if order:
                go[k]["status"] = "sold"
                go[k]["sell_order"] = order

    s["grid_orders"] = go
    save_state(s)
    logger.info(f"tick | price={int(current):,} | auto={s.get('auto_mode')} | test={s.get('test_mode')}")

# ---------- Scheduler ----------
def loop_runner():
    logger.info("Loop runner started")
    while True:
        try:
            with state_lock:
                s = load_state()
                interval = s.get("check_interval", CHECK_INTERVAL)
            run_grid_once()
            time.sleep(interval)
        except Exception as e:
            logger.exception(f"loop err: {e}")
            time.sleep(3)

# ---------- News helpers ----------
def news_fetch_from_sources(sources):
    items = []
    for name in sources:
        url = RSS_MAP.get(name)
        if not url:
            continue
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:20]:
                eid = getattr(e, "id", None) or getattr(e, "link", None) or getattr(e, "title", "")[:80]
                title = e.title if hasattr(e, "title") else ""
                link = e.link if hasattr(e, "link") else ""
                summary = getattr(e, "summary", "") or getattr(e, "description", "")
                published = None
                if hasattr(e, "published_parsed") and e.published_parsed:
                    published = datetime(*e.published_parsed[:6]).astimezone(LOCAL_TZ)
                items.append({"id": f"{name}:{eid}","source": name,"title": title,"link": link,"summary": summary,"published": published.isoformat() if published else None})
        except Exception as ex:
            logger.warning(f"RSS fetch fail {name}: {ex}")
    return items

def news_filter_items(items, include_keywords):
    if not include_keywords:
        return items
    keys = [k.lower() for k in include_keywords]
    filtered = []
    for it in items:
        text = f"{it['title']} {it['summary']}".lower()
        if any(k in text for k in keys):
            filtered.append(it)
    return filtered

POS_KEYS = ["etf", "approval", "adoption", "institution", "upgrade", "partnership", "bull", "long"]
NEG_KEYS = ["hack", "ban", "regulation", "lawsuit", "down", "restrict", "selloff", "liquidation", "bear", "short"]

def news_recommend_strategy(item):
    text = f"{item['title']} {item['summary']}".lower()
    pos = sum(k in text for k in POS_KEYS)
    neg = sum(k in text for k in NEG_KEYS)
    if neg > pos:
        return "down", "⚠️ 리스크 확대 가능성 — 보수적(down) 권장"
    if pos > neg:
        return "up", "✅ 호재 가능성 — 적극적(up) 권장"
    return "middle", "ℹ️ 중립 — 횡보(middle) 유지 권장"

def tg_send_news_item(item):
    strat, note = news_recommend_strategy(item)
    title = item['title']
    link = item['link']
    pub_s = ""
    if item.get("published"):
        pub_s = f"\n🕒 {item['published']}"
    msg = f"📰 [{item['source']}] {title}{pub_s}\n{link}\n\n전략 제안: {note}\n바꾸기 → /strategy {strat}"
    tg_send(msg)

def news_loop():
    logger.info(f"News loop started | interval={NEWS_INTERVAL_MIN}m | sources={NEWS_SOURCES}")
    while True:
        try:
            with state_lock:
                s = load_state()
            if not s.get("news_enabled", False):
                time.sleep(NEWS_INTERVAL_MIN * 60)
                continue

            items = news_fetch_from_sources(NEWS_SOURCES)
            items = news_filter_items(items, s.get("news_filter", []))

            seen = set(s.get("news_seen_ids", []))
            fresh = [it for it in items if it["id"] not in seen]
            fresh = fresh[:NEWS_MAX_ITEMS]

            for it in fresh:
                tg_send_news_item(it)
                seen.add(it["id"])

            with state_lock:
                s2 = load_state()
                s2["news_seen_ids"] = list(seen)[-1000:]
                save_state(s2)

            time.sleep(NEWS_INTERVAL_MIN * 60)

        except Exception as e:
            logger.warning(f"news loop err: {e}")
            time.sleep(30)

# ---------- Telegram polling (commands) ----------
def telegram_poll():
    if not TELEGRAM_API or not TELEGRAM_CHAT_ID:
        logger.info("Telegram not configured; polling disabled")
        return
    offset = None
    while True:
        try:
            params = {"timeout": 25}
            if offset:
                params["offset"] = offset
            r = requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=30)
            if r.ok:
                js = r.json()
                for u in js.get("result", []):
                    offset = u["update_id"] + 1
                    if "callback_query" in u:
                        cb = u["callback_query"]
                        data = json.loads(cb.get("data"))
                        telegram_answers[data["id"]] = (data["ans"], datetime.utcnow().isoformat())
                        requests.post(f"{TELEGRAM_API}/answerCallbackQuery", data={"callback_query_id": cb["id"]})
                    elif "message" in u and "text" in u["message"]:
                        text = u["message"]["text"].strip()
                        with state_lock:
                            s = load_state()
                            if text.startswith("/auto"):
                                s["auto_mode"] = True; save_state(s); tg_send("자동 승인 모드 ON")
                            elif text.startswith("/manual"):
                                s["auto_mode"] = False; save_state(s); tg_send("수동 승인 모드 ON")
                            elif text.startswith("/restart"):
                                s["auto_mode"] = True; save_state(s); tg_send("자동매매 재시작 (AUTO_MODE=ON)")
                            elif text.startswith("/stop"):
                                tg_send("자동매매 종료합니다."); save_state(s); os._exit(0)
                            elif text.startswith("/balance"):
                                tg_send(f"잔액\nKRW: {s.get('krw'):,}\nBTC: {s.get('btc')}")
                            elif text.startswith("/current_target"):
                                go = s.get("grid_orders", {}); last = None
                                for k, v in go.items():
                                    if v.get("status") == "bought":
                                        last = (k, v)
                                if last:
                                    k, g = last
                                    tg_send(f"마지막 매수 Grid #{k}\n매수가: {int(g['buy_price']):,}\n목표가: {int(g['sell_price']):,}\n수량: {g['amount']}")
                                else:
                                    tg_send("진행 중 포지션 없음")
                            elif text.startswith("/set_target"):
                                parts = text.split()
                                if len(parts) == 2 and parts[1].replace(".","",1).isdigit():
                                    target = float(parts[1])
                                    go = s.get("grid_orders", {})
                                    if go:
                                        last_key = max(go.keys(), key=lambda x:int(x))
                                        go[last_key]["sell_price"] = target
                                        save_state(s)
                                        tg_send(f"그리드 #{last_key} 목표가 {int(target):,}으로 변경")
                            elif text.startswith("/test_on"):
                                s["test_mode"] = True; save_state(s); tg_send("테스트 모드 ON (랜덤 시세)")
                            elif text.startswith("/test_off"):
                                s["test_mode"] = False; save_state(s); tg_send("테스트 모드 OFF (실시세 시도)")
                            elif text.startswith("/mode"):
                                tg_send(f"MODE\nAUTO_MODE: {s.get('auto_mode')}\nTEST_MODE: {s.get('test_mode')}")
                            # --- 뉴스 명령 ---
                            elif text.startswith("/news_on"):
                                s["news_enabled"] = True; save_state(s); tg_send("🟢 뉴스 알림 ON")
                            elif text.startswith("/news_off"):
                                s["news_enabled"] = False; save_state(s); tg_send("⚪️ 뉴스 알림 OFF")
                            elif text.startswith("/news_now"):
                                items = news_fetch_from_sources(NEWS_SOURCES)
                                items = news_filter_items(items, s.get("news_filter", []))
                                sent = 0
                                seen = set(s.get("news_seen_ids", []))
                                for it in items:
                                    if it["id"] in seen:
                                        continue
                                    tg_send_news_item(it)
                                    seen.add(it["id"]); sent += 1
                                    if sent >= NEWS_MAX_ITEMS:
                                        break
                                s["news_seen_ids"] = list(seen)[-1000:]
                                save_state(s)
                                tg_send(f"즉시 뉴스 {sent}건 전송 완료")
                            elif text.startswith("/news_filter"):
                                parts = text.split(" ", 1)
                                if len(parts) == 2:
                                    kws = [k.strip().lower() for k in parts[1].split(",") if k.strip()]
                                    s["news_filter"] = kws
                                    save_state(s)
                                    tg_send(f"뉴스 필터 업데이트: {', '.join(kws) if kws else '(전체)'}")
                                else:
                                    tg_send("사용법: /news_filter 키워드1,키워드2  (비우면 전체)")
                            elif text.startswith("/news"):
                                conf = (f"뉴스 알림: {s.get('news_enabled')}\n"
                                        f"소스: {', '.join(NEWS_SOURCES)}\n"
                                        f"필터: {', '.join(s.get('news_filter', [])) or '(전체)'}\n"
                                        f"주기: {NEWS_INTERVAL_MIN}분 / 최대 {NEWS_MAX_ITEMS}건")
                                tg_send(conf + "\n\n즉시 받기: /news_now\nON: /news_on  OFF: /news_off\n필터변경: /news_filter bitcoin,btc")
                            # --- 전략 프리셋 ---
                            elif text.startswith("/strategy_show"):
                                key = s.get("strategy")
                                if key and key in STRATEGY_PROFILES:
                                    prof = STRATEGY_PROFILES[key]
                                    low  = s.get("price_low")
                                    high = s.get("price_high")
                                    n    = s.get("n_grids", N_GRIDS)
                                    pad  = s.get("price_padding", PRICE_PADDING)
                                    itv  = s.get("check_interval", CHECK_INTERVAL)
                                    tg_send(f"현재 전략: {prof['name']} ({key})\n범위: {int(low):,} ~ {int(high):,}\nN_GRIDS: {n} | PADDING: {pad} | INTERVAL: {itv}s")
                                else:
                                    tg_send("현재 전략 프리셋 없음. /strategy up|middle|down 로 설정")
                            elif text.startswith("/strategy"):
                                parts = text.split()
                                if len(parts) == 2 and parts[1].lower() in STRATEGY_PROFILES:
                                    key = parts[1].lower()
                                    curr = get_price(SYMBOL)
                                    summary = apply_strategy_profile(s, curr, key)
                                    save_state(s)
                                    if summary:
                                        tg_send("✅ 전략이 변경되었습니다.\n" + summary + "\n다음 tick부터 적용됩니다.")
                                    else:
                                        tg_send("전략 적용 실패")
                                else:
                                    tg_send("사용법: /strategy up | /strategy middle | /strategy down")
            time.sleep(1)
        except Exception as e:
            logger.warning(f"telegram poll err: {e}")
            time.sleep(2)

# ---------- Flask app ----------
app = Flask(__name__)

@app.route("/")
def home():
    with state_lock:
        s = load_state()
    return f"Grid Trader running | AUTO_MODE={s.get('auto_mode')} TEST_MODE={s.get('test_mode')}"

@app.route("/status")
def status():
    with state_lock:
        s = load_state()
    return jsonify(s)

@app.route("/tick")
def tick():
    run_grid_once()
    return jsonify({"ok": True, "ts": datetime.utcnow().isoformat()})

@app.route("/price")
def price():
    p = get_price(SYMBOL)
    return jsonify({"price": p, "symbol": SYMBOL})

def run_web():
    app.run(host="0.0.0.0", port=PORT)

# ---------- Keep-alive (optional) ----------
def keep_alive():
    url = os.getenv("PUBLIC_URL")
    if not url:
        return
    while True:
        try:
            requests.get(url, timeout=10)
        except Exception:
            pass
        time.sleep(240)

# ---------- Boot ----------
if __name__ == "__main__":
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        Thread(target=telegram_poll, daemon=True).start()
        logger.info("Telegram poll started")
    else:
        logger.info("Telegram not configured")

    Thread(target=news_loop, daemon=True).start()
    logger.info("News loop thread started")

    Thread(target=run_web, daemon=True).start()
    logger.info(f"Flask running on :{PORT}")

    Thread(target=keep_alive, daemon=True).start()

    loop_runner()
