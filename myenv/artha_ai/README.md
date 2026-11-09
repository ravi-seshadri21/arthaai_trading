# ARTHA AI Trading System
### Python + SQLite + FastAPI + Claude AI

## Tech Stack
- **Backend:** Python + FastAPI
- **Database:** SQLite (built-in, zero setup)
- **AI Engine:** Claude (Anthropic API)
- **Frontend:** Vanilla HTML/CSS/JS (served by FastAPI)
- **Markets:** CoinDCX (Crypto) + Upstox (Stocks) + Groww (Stocks/MF)

---

## Quick Start (3 steps)

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Set your Anthropic API key
```bash
# Mac/Linux
export ANTHROPIC_API_KEY=sk-ant-your-key-here

# Windows (Command Prompt)
set ANTHROPIC_API_KEY=sk-ant-your-key-here

# Windows (PowerShell)
$env:ANTHROPIC_API_KEY="sk-ant-your-key-here"
```
Get your key at: https://console.anthropic.com

### 3. Run the server
```bash
python app.py
```

Open your browser at: **http://localhost:8000**

---

## Database (SQLite)

The file `artha_trading.db` is created automatically on first run.

### Tables
| Table | Description |
|---|---|
| `prices` | Tick-by-tick price history (every 5 seconds) |
| `signals` | All AI-generated trading signals |
| `trades` | Simulated trade executions |
| `portfolio` | Current holdings and P&L |
| `api_keys` | Exchange API keys (stored locally) |
| `activity_logs` | System activity log |

### Query the database directly
```bash
# Open SQLite shell
sqlite3 artha_trading.db

# View recent signals
SELECT asset, signal, confidence, entry_price, target_price, stop_loss, created_at
FROM signals ORDER BY created_at DESC LIMIT 10;

# View portfolio
SELECT asset, quantity, avg_buy_price, current_price FROM portfolio;

# View trade history
SELECT asset, trade_type, quantity, price, total_value, created_at FROM trades;

# Exit
.quit
```

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/prices` | Latest prices for all assets |
| POST | `/api/analyze` | Get AI signal for an asset |
| GET | `/api/signals` | Signal history |
| POST | `/api/trade` | Simulate a trade |
| GET | `/api/portfolio` | Portfolio with live P&L |
| GET | `/api/trades` | Trade history |
| POST | `/api/chat` | AI trading advisor chat |
| GET/POST | `/api/keys` | Manage exchange API keys |
| GET/DELETE | `/api/logs` | Activity logs |

---

## Adding Live Prices (Next Step)

To connect real market data, replace the `simulate_prices()` function in `app.py`:

### CoinDCX (Crypto)
```python
import requests

def fetch_coindcx_price(pair):
    res = requests.get(f"https://api.coindcx.com/exchange/ticker")
    data = res.json()
    for item in data:
        if item['market'] == pair.replace('/','_'):
            return float(item['last_price'])
```

### Upstox (Stocks) — requires OAuth token
```python
# After OAuth flow, use:
headers = {"Authorization": f"Bearer {access_token}"}
res = requests.get(f"https://api.upstox.com/v2/market-quote/ltp?instrument_key=NSE_EQ|{symbol}", headers=headers)
```

---

## Disclaimer
This system is for **educational and simulation purposes only**.
- Not SEBI-registered investment advice
- Crypto and stock trading involves significant financial risk
- Always do your own research before investing
- Never invest more than you can afford to lose
