# Binance Liquidation Monitor (3 pairs)

Live liquidation feed (XRP/DOGE/PEPE), AI window signal, prices, and paper trading (Binance Futures Testnet or simulated).

## Run local
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # edit if needed
uvicorn backend.server_liq:app --host 0.0.0.0 --port 8000
