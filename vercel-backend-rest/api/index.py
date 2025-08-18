
import os
from typing import Dict, Any, Union, Tuple

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from binance.spot import Spot as BinanceSpot

load_dotenv()

API_TOKEN = os.getenv("API_TOKEN")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
ALLOWED_SYMBOLS = {s.strip().upper() for s in os.getenv("ALLOWED_SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT").split(",") if s.strip()}
BINANCE_BASE_URL = "https://testnet.binance.vision"

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

def _auth(x_api_token: str | None):
    if not API_TOKEN or x_api_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

class BinanceClient:
    def __init__(self) -> None:
        self.client = BinanceSpot(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET, base_url=BINANCE_BASE_URL)
        self.rules: Dict[str, Dict[str, Any]] = {}
        self._load_rules()

    def _load_rules(self) -> None:
        info = self.client.exchange_info()
        for s in info.get("symbols", []):
            symbol = s.get("symbol")
            if not symbol:
                continue
            filters = {f["filterType"]: f for f in s.get("filters", [])}
            lot = filters.get("LOT_SIZE", {})
            tick = filters.get("PRICE_FILTER", {})
            self.rules[symbol] = {
                "stepSize": float(lot.get("stepSize", 0.0)) if lot else 0.0,
                "tickSize": float(tick.get("tickSize", 0.0)) if tick else 0.0,
            }

    def _round_step(self, value: float, step: float) -> float:
        if step == 0:
            return value
        from math import floor, log10
        precision = int(round(-log10(step))) if step < 1 else 0
        return float(f"{(floor(value / step) * step):.{precision}f}")

    def _round_for(self, symbol: str, qty: float, price: Union[float, None] = None) -> Tuple[float, Union[float, None]]:
        r = self.rules.get(symbol, {})
        step = r.get("stepSize", 0.0) or 0.0
        tick = r.get("tickSize", 0.0) or 0.0
        if step:
            qty = self._round_step(qty, step)
        if price is not None and tick:
            price = self._round_step(price, tick)
        return qty, price

    def price(self, symbol: str) -> float:
        data = self.client.ticker_price(symbol)
        return float(data["price"])

    def balance(self) -> Dict[str, float]:
        acct = self.client.account()
        out: Dict[str, float] = {}
        for b in acct.get("balances", []):
            free = float(b.get("free", 0))
            locked = float(b.get("locked", 0))
            total = free + locked
            if total > 0:
                out[b["asset"]] = total
        return out

    def order_market(self, symbol: str, side: str, quantity: float) -> Dict[str, Any]:
        q, _ = self._round_for(symbol, quantity)
        return self.client.new_order(symbol=symbol, side=side, type="MARKET", quantity=q)

    def order_limit(self, symbol: str, side: str, quantity: float, price: float, tif: str = "GTC") -> Dict[str, Any]:
        q, p = self._round_for(symbol, quantity, price)
        if p is None:
            raise ValueError("price required")
        return self.client.new_order(symbol=symbol, side=side, type="LIMIT", timeInForce=tif, quantity=q, price=f"{p}")

    def order_oco(self, symbol: str, side: str, quantity: float, take_profit: float, stop_price: float, stop_limit: Union[float, None] = None, tif: str = "GTC") -> Dict[str, Any]:
        q, tp = self._round_for(symbol, quantity, take_profit)
        _, sp = self._round_for(symbol, quantity, stop_price)
        if stop_limit is None:
            tick = self.rules.get(symbol, {}).get("tickSize", 0.0)
            stop_limit = (sp or stop_price) - (tick * 2 if tick else 0.0001)
        _, sl = self._round_for(symbol, quantity, stop_limit)
        return self.client.new_oco_order(symbol=symbol, side=side, quantity=q, price=f"{tp}", stopPrice=f"{sp}", stopLimitPrice=f"{sl}", stopLimitTimeInForce=tif)

binance = BinanceClient()

def _sanitize_symbol(s: str) -> str:
    return s.replace(" ", "").upper()

@app.get("/price")
def price(symbol: str, x_api_token: str | None = Header(default=None)):
    _auth(x_api_token)
    symbol = _sanitize_symbol(symbol)
    if ALLOWED_SYMBOLS and symbol not in ALLOWED_SYMBOLS:
        raise HTTPException(status_code=400, detail="Symbol not allowed")
    return {"symbol": symbol, "price": binance.price(symbol)}

@app.get("/balance")
def balance(x_api_token: str | None = Header(default=None)):
    _auth(x_api_token)
    return {"balances": binance.balance()}

@app.post("/buy")
def buy(payload: Dict[str, Any], x_api_token: str | None = Header(default=None)):
    _auth(x_api_token)
    symbol = _sanitize_symbol(str(payload.get("symbol", "")))
    qty = float(payload.get("qty", 0))
    order_type = str(payload.get("type", "market")).lower()
    if ALLOWED_SYMBOLS and symbol not in ALLOWED_SYMBOLS:
        raise HTTPException(status_code=400, detail="Symbol not allowed")
    if order_type == "market":
        resp = binance.order_market(symbol, "BUY", qty)
    elif order_type == "limit":
        price = float(payload.get("price"))
        resp = binance.order_limit(symbol, "BUY", qty, price)
    else:
        raise HTTPException(status_code=400, detail="Unknown order type")
    return {"ok": True, "response": resp}

@app.post("/sell")
def sell(payload: Dict[str, Any], x_api_token: str | None = Header(default=None)):
    _auth(x_api_token)
    symbol = _sanitize_symbol(str(payload.get("symbol", "")))
    qty = float(payload.get("qty", 0))
    order_type = str(payload.get("type", "market")).lower()
    if ALLOWED_SYMBOLS and symbol not in ALLOWED_SYMBOLS:
        raise HTTPException(status_code=400, detail="Symbol not allowed")
    if order_type == "market":
        resp = binance.order_market(symbol, "SELL", qty)
    elif order_type == "limit":
        price = float(payload.get("price"))
        resp = binance.order_limit(symbol, "SELL", qty, price)
    else:
        raise HTTPException(status_code=400, detail="Unknown order type")
    return {"ok": True, "response": resp}

@app.post("/oco")
def oco(payload: Dict[str, Any], x_api_token: str | None = Header(default=None)):
    _auth(x_api_token)
    symbol = _sanitize_symbol(str(payload.get("symbol", "")))
    qty = float(payload.get("qty", 0))
    tp = float(payload.get("tp"))
    sp = float(payload.get("sp"))
    sl = payload.get("sl")
    slf = float(sl) if sl is not None else None
    if ALLOWED_SYMBOLS and symbol not in ALLOWED_SYMBOLS:
        raise HTTPException(status_code=400, detail="Symbol not allowed")
    resp = binance.order_oco(symbol, "SELL", qty, tp, sp, slf)
    return {"ok": True, "response": resp}

@app.get("/")
def health():
    return {"ok": True}
