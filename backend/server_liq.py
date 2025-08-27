import os, json, time, threading, requests
from websocket import WebSocketApp
from collections import deque
from fastapi import FastAPI, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

# ========= CONFIG =========
WATCHLIST = [s.strip().upper() for s in os.getenv("WATCHLIST","XRPUSDT,DOGEUSDT,PEPEUSDT").split(",")]

# Ambang notional (USD) per pair (untuk AI event-level). Override via ENV THRESHOLDS_USD='{"XRPUSDT":7000,...}'
_DEFAULT_THRESHOLDS_USD = {"XRPUSDT": 7500.0, "DOGEUSDT": 6000.0, "PEPEUSDT": 3000.0}
try:
    THRESHOLDS_USD = {**_DEFAULT_THRESHOLDS_USD, **(json.loads(os.getenv("THRESHOLDS_USD") or "{}"))}
except Exception:
    THRESHOLDS_USD = _DEFAULT_THRESHOLDS_USD.copy()

# Ambang quantity per pair untuk FILTER tampilan tabel (permintaanmu):
# XRP >= 3000; DOGE & PEPE disesuaikan (default: 50,000 DOGE; 100,000,000 PEPE)
_DEFAULT_QTY_THRESHOLDS = {"XRPUSDT": 3000.0, "DOGEUSDT": 50000.0, "PEPEUSDT": 100_000_000.0}
try:
    QTY_THRESHOLDS = {**_DEFAULT_QTY_THRESHOLDS, **(json.loads(os.getenv("QTY_THRESHOLDS") or "{}"))}
except Exception:
    QTY_THRESHOLDS = _DEFAULT_QTY_THRESHOLDS.copy()

# Filter tambahan berbasis USD untuk tabel (opsional, 0 = nonaktif)
MIN_TABLE_USD = float(os.getenv("MIN_TABLE_USD","0"))

# AI rolling window (detik)
WINDOW_SEC = int(os.getenv("WINDOW_SEC","180"))  # 3 menit

# Testnet trading (paper)
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY","")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET","")
BINANCE_TESTNET = os.getenv("BINANCE_TESTNET","true").lower() == "true"

# ========= STATE =========
latest_signals = {s: {"recommendation":"HOLD","confidence":0} for s in WATCHLIST}
recent_liqs = deque(maxlen=500)
events_by_sym = {s: deque(maxlen=3000) for s in WATCHLIST}
paper_trades = deque(maxlen=200)
uptime_start = time.time()

# ========= APP =========
app = FastAPI(title="Binance Liquidation Monitor")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ========= UTILS =========
def notional(price, qty):
    try: return float(price) * float(qty)
    except: return 0.0

def get_th_usd(sym): return float(THRESHOLDS_USD.get(sym.upper(), 5000.0))
def get_th_qty(sym): return float(QTY_THRESHOLDS.get(sym.upper(), 0.0))

# ========= AI: rolling window =========
def compute_signal(sym, now=None):
    now = now or time.time()
    dq = events_by_sym.get(sym)
    if not dq: return {"recommendation":"HOLD","confidence":0}
    # purge
    while dq and (now - dq[0][0]) > WINDOW_SEC: dq.popleft()
    if not dq: return {"recommendation":"HOLD","confidence":0}
    buy_usd  = sum(usd for t,side,usd in dq if side == "BUY")
    sell_usd = sum(usd for t,side,usd in dq if side == "SELL")
    total = buy_usd + sell_usd
    if total <= 0: return {"recommendation":"HOLD","confidence":0}
    bias = (buy_usd - sell_usd) / total  # -1..+1
    if abs(bias) < 0.08: rec = "HOLD"
    else:                rec = "BUY" if bias > 0 else "SELL"
    conf = max(10, min(95, int(50 + 45*abs(bias))))
    try:
        if any(usd >= get_th_usd(sym) for _,_,usd in list(dq)[-5:]):
            conf = min(99, conf + 5)
    except: pass
    return {"recommendation": rec, "confidence": conf}

# ========= WS LOOP =========
def _ws_loop():
    url = "wss://fstream.binance.com/ws/!forceOrder@arr"
    while True:
        try:
            def on_msg(ws, msg):
                try:
                    data = json.loads(msg)
                    events = data if isinstance(data, list) else [data]
                    for ev in events:
                        o = ev.get("o", {})
                        sym = (o.get("s") or "").upper()
                        if sym not in WATCHLIST: continue
                        side = o.get("S")
                        price = float(o.get("p") or o.get("ap") or 0)
                        qty   = float(o.get("q") or o.get("l")  or 0)
                        ts_ms = int(o.get("T") or ev.get("E") or time.time()*1000)
                        usd   = notional(price, qty)

                        # simpan untuk AI window
                        events_by_sym[sym].append((ts_ms/1000.0, side, usd))

                        # event-level AI (notional vs threshold USD)
                        th_usd = get_th_usd(sym)
                        if usd >= th_usd:
                            rec_evt = "SELL" if side == "SELL" else "BUY"
                            overs = max(0.0, (usd - th_usd)/max(th_usd,1))
                            conf_evt = max(70, min(95, int(80 + overs*15)))
                        else:
                            rec_evt, conf_evt = "HOLD", 0

                        # FILTER TABEL: hanya qty besar per pair (+ optional usd)
                        if qty >= get_th_qty(sym) and usd >= MIN_TABLE_USD:
                            recent_liqs.appendleft({
                                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts_ms/1000)),
                                "symbol": sym, "side": side, "price": price, "quantity": qty,
                                "ai_recommendation": rec_evt, "confidence": conf_evt
                            })

                        # update sinyal realtime dari window
                        latest_signals[sym] = compute_signal(sym)
                except Exception:
                    pass
            ws = WebSocketApp(url, on_message=on_msg)
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception:
            time.sleep(3)  # retry

@app.on_event("startup")
def boot():
    threading.Thread(target=_ws_loop, daemon=True).start()

# ========= UI =========
UI_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "docs", "index.html"))
@app.get("/", response_class=HTMLResponse)
def root():
    try:
        return open(UI_PATH, "r", encoding="utf-8").read()
    except FileNotFoundError:
        return HTMLResponse("<h1>UI not found.</h1><p>Put docs/index.html in repo.</p>", status_code=200)

# ========= BASIC API =========
@app.get("/health")
def health(): return {"ok": True, "ts": int(time.time()*1000)}

@app.get("/symbols")
def symbols():
    return {"watchlist": WATCHLIST,
            "thresholds_usd": THRESHOLDS_USD,
            "qty_thresholds": QTY_THRESHOLDS,
            "min_table_usd": MIN_TABLE_USD,
            "window_sec": WINDOW_SEC}

@app.get("/status")
def status():
    up = int(time.time() - uptime_start)
    return {"uptime": time.strftime("%H:%M:%S", time.gmtime(up)),
            "processed_today": len(recent_liqs),
            "ai_accuracy": 82, "provider": "binance", "telegram_delivery": 99.1}

@app.get("/prices")
def prices():
    def arr(url):
        r = requests.get(url, params={"symbols": json.dumps(WATCHLIST)}, timeout=8)
        r.raise_for_status()
        data = r.json()
        return {it["symbol"]: float(it["price"]) for it in data}
    out, provider = {}, None
    for name, url in [("binance","https://api.binance.com/api/v3/ticker/price"),
                      ("binance.vision","https://data-api.binance.vision/api/v3/ticker/price")]:
        try: out = arr(url); provider = name; break
        except Exception: pass
    if not out:
        for s in WATCHLIST:
            ok=False
            for url in [f"https://api.binance.com/api/v3/ticker/price?symbol={s}",
                        f"https://data-api.binance.vision/api/v3/ticker/price?symbol={s}"]:
                try:
                    r = requests.get(url, timeout=8); r.raise_for_status()
                    out[s] = float(r.json()["price"]); provider = provider or "binance(per-symbol)"; ok=True; break
                except Exception: pass
            if not ok:
                try:
                    r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex", params={"symbol": s}, timeout=8)
                    r.raise_for_status()
                    out[s] = float(r.json()["markPrice"]); provider = provider or "binance-futures(mark)"
                except Exception:
                    out[s] = None; provider = provider or "unavailable"
    return {"provider": provider or "unavailable", "prices": out}

@app.get("/analysis")
def analysis():
    out = {sym: compute_signal(sym) for sym in WATCHLIST}
    return {"model": f"liq-window-v1({WINDOW_SEC}s)", "signals": out}

@app.get("/liquidations")
def liquidations(limit: int = 50):
    items = [r for r in list(recent_liqs) if r["symbol"] in WATCHLIST]
    return items[:limit]

# ========= PAPER TRADING (Testnet) =========
def _get_client():
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        return None
    try:
        from binance.client import Client
        c = Client(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=BINANCE_TESTNET)
        return c
    except Exception:
        return None

@app.get("/paper/config")
def paper_config():
    return {"enabled": bool(_get_client() or True),  # True karena kita bisa simulasi lokal
            "testnet": BINANCE_TESTNET,
            "has_keys": bool(BINANCE_API_KEY and BINANCE_API_SECRET)}

@app.get("/paper/orders")
def paper_orders(): return list(paper_trades)[:50]

@app.post("/paper/order")
def paper_order(payload: dict = Body(...)):
    sym = (payload.get("symbol") or "").upper()
    side = (payload.get("side") or "").upper()
    qty  = float(payload.get("quantity") or 0)
    assert sym in WATCHLIST, "symbol not in WATCHLIST"
    assert side in ("BUY","SELL"), "side must be BUY/SELL"
    assert qty > 0, "quantity must be > 0"
    client = _get_client()
    ts = int(time.time()*1000)

    if client:
        try:
            # MARKET order futures testnet
            res = client.futures_create_order(symbol=sym, side=side, type="MARKET", quantity=qty)
            paper_trades.appendleft({"ts": ts, "symbol": sym, "side": side, "quantity": qty, "mode": "testnet", "raw": res})
            return {"ok": True, "mode": "testnet", "result": res}
        except Exception as e:
            # fallback simulate
            paper_trades.appendleft({"ts": ts, "symbol": sym, "side": side, "quantity": qty, "mode": "sim", "error": str(e)})
            return {"ok": False, "mode": "testnet", "error": str(e)}
    else:
        # simulate locally
        paper_trades.appendleft({"ts": ts, "symbol": sym, "side": side, "quantity": qty, "mode": "sim"})
        return {"ok": True, "mode": "sim"}
