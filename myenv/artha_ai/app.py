"""
ARTHA AI Trading System — Multi-User
Run: uvicorn app:app --reload
"""
import sqlite3, json, os, random, time, hmac, hashlib, urllib.parse, secrets
from datetime import datetime, timedelta
from collections import deque
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt  # pip install PyJWT

# ── GEMINI RATE LIMITER (Free tier: 15 RPM / 1500 RPD) ───────────────────────
_gemini_calls    = deque()   # timestamps of recent calls
_gemini_day_calls= deque()   # timestamps for daily count
GEMINI_RPM_LIMIT = 15
GEMINI_RPD_LIMIT = 1500
_gemini_lock     = __import__("threading").Lock()

def gemini_rate_check():
    """
    Enforces 15 RPM and 1500 RPD limits before any Gemini API call.
    Raises HTTPException 429 with clear message if limit would be exceeded.
    """
    now = time.time()
    with _gemini_lock:
        # Clean old entries outside windows
        while _gemini_calls and now - _gemini_calls[0] > 60:
            _gemini_calls.popleft()
        while _gemini_day_calls and now - _gemini_day_calls[0] > 86400:
            _gemini_day_calls.popleft()

        rpm_used = len(_gemini_calls)
        rpd_used = len(_gemini_day_calls)

        if rpm_used >= GEMINI_RPM_LIMIT:
            wait = round(60 - (now - _gemini_calls[0]), 1)
            raise HTTPException(429, f"Gemini rate limit: {rpm_used}/{GEMINI_RPM_LIMIT} requests this minute. Wait {wait}s and try again.")
        if rpd_used >= GEMINI_RPD_LIMIT:
            raise HTTPException(429, f"Gemini daily limit reached: {rpd_used}/{GEMINI_RPD_LIMIT} requests today. Resets at midnight Pacific Time.")

        _gemini_calls.append(now)
        _gemini_day_calls.append(now)

def gemini_usage():
    """Returns current RPM and RPD usage for display."""
    now = time.time()
    with _gemini_lock:
        rpm = sum(1 for t in _gemini_calls if now - t < 60)
        rpd = sum(1 for t in _gemini_day_calls if now - t < 86400)
    return {"rpm_used": rpm, "rpm_limit": GEMINI_RPM_LIMIT,
            "rpd_used": rpd, "rpd_limit": GEMINI_RPD_LIMIT,
            "rpm_remaining": GEMINI_RPM_LIMIT - rpm,
            "rpd_remaining": GEMINI_RPD_LIMIT - rpd}
import google.generativeai as genai
from pydantic import BaseModel
import uvicorn, threading
import requests as http_requests

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
DB_PATH        = os.path.join(BASE_DIR, "artha_trading.db")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyAddosqF4e3E2qeGbaNYZlwffqnpNhUBaU")

# ── AUTH CONFIG ───────────────────────────────────────────────────────────────
# JWT secret — override via env var in production: export JWT_SECRET=your-long-random-string
JWT_SECRET     = os.getenv("JWT_SECRET", secrets.token_hex(32))
JWT_ALGORITHM  = "HS256"
JWT_EXPIRE_DAYS= 7          # tokens expire after 7 days
security       = HTTPBearer(auto_error=False)

def hash_password(password: str) -> str:
    """SHA-256 hash with salt for password storage."""
    salt = "artha_ai_salt_2026"
    return hashlib.sha256((salt + password).encode()).hexdigest()

def create_token(user_id: int, email: str) -> str:
    payload = {
        "user_id": user_id,
        "email":   email,
        "exp":     datetime.utcnow() + timedelta(days=JWT_EXPIRE_DAYS)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Session expired — please log in again")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid session — please log in again")

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """FastAPI dependency — extracts user from JWT token in Authorization header."""
    if not credentials:
        raise HTTPException(401, "Not authenticated")
    return decode_token(credentials.credentials)

def get_current_user_optional(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Same as above but returns None instead of raising for public routes."""
    if not credentials:
        return None
    try:
        return decode_token(credentials.credentials)
    except Exception:
        return None

# ── DYNAMIC BASE PRICES ───────────────────────────────────────────────────────
# These are loaded from live Upstox/Groww holdings after first sync.
# Fallback prices used only until first sync completes.
BASE_PRICES = {
    "BTC/INR":6800000,"ETH/INR":350000,"SOL/INR":18000,
    "BNB/INR":52000,"XRP/INR":55,
    "NIFTY50":24200,"SENSEX":79800,
}
PLATFORMS = {
    "BTC/INR":"CoinDCX","ETH/INR":"CoinDCX","SOL/INR":"CoinDCX",
    "BNB/INR":"CoinDCX","XRP/INR":"CoinDCX",
    "NIFTY50":"Groww","SENSEX":"Groww",
}

def load_prices_from_db():
    """Load holdings from DB into BASE_PRICES and PLATFORMS for price simulation."""
    try:
        conn = get_db()
        rows = conn.execute("SELECT asset, platform, avg_buy_price FROM portfolio WHERE avg_buy_price > 0").fetchall()
        conn.close()
        for r in rows:
            if r["asset"] not in BASE_PRICES:
                BASE_PRICES[r["asset"]] = float(r["avg_buy_price"])
            if r["asset"] not in PLATFORMS:
                PLATFORMS[r["asset"]] = r["platform"]
    except Exception:
        pass

SYSTEM_PROMPT = """You are an expert AI trading analyst for Indian markets.
Risk profile: MODERATE. Prefer LOW-risk, high-confidence (>70%) signals. Preserve capital first.
Respond ONLY in this exact JSON format (no markdown, no extra text):
{
  "signal": "BUY"|"SELL"|"HOLD",
  "confidence": <0-100>,
  "entry_price": <number>,
  "target_price": <number>,
  "stop_loss": <number>,
  "reasoning": "<2-3 sentences>",
  "risk_level": "LOW"|"MODERATE"|"HIGH",
  "strategy": "<strategy name>",
  "time_horizon": "<e.g. 1-3 days>"
}"""

AUTO_TRADE_PROMPT = """You are an autonomous AI trading agent for Indian markets.
Analyse ALL holdings and provide trade decisions. For each asset decide BUY/SELL/HOLD.
Only recommend BUY or SELL when confidence > 75% and risk is LOW or MODERATE.
You MUST explain every decision clearly so the user can review before execution.
Respond ONLY in this JSON format:
{
  "decisions": [
    {
      "asset": "<symbol>",
      "platform": "<platform>",
      "signal": "BUY"|"SELL"|"HOLD",
      "confidence": <0-100>,
      "suggested_qty": <number>,
      "entry_price": <number>,
      "target_price": <number>,
      "stop_loss": <number>,
      "risk_level": "LOW"|"MODERATE"|"HIGH",
      "reasoning": "<clear explanation for user>",
      "time_horizon": "<e.g. 1-3 days>"
    }
  ],
  "summary": "<overall market summary for user>",
  "total_actionable": <count of BUY+SELL decisions>
}"""

# ── DB ─────────────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db(); c = conn.cursor()

    # ── Users table ───────────────────────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        name TEXT NOT NULL,
        role TEXT DEFAULT 'user',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_login TIMESTAMP)""")

    # ── Per-user tables (all include user_id) ─────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS prices (
        id INTEGER PRIMARY KEY AUTOINCREMENT, asset TEXT, platform TEXT,
        price REAL, change_pct REAL, recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")

    c.execute("""CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
        asset TEXT, platform TEXT, signal TEXT, confidence INTEGER,
        entry_price REAL, target_price REAL, stop_loss REAL, reasoning TEXT,
        risk_level TEXT, strategy TEXT, time_horizon TEXT, price_at_signal REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")

    c.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
        asset TEXT, platform TEXT, trade_type TEXT, quantity REAL,
        price REAL, total_value REAL, status TEXT DEFAULT 'SIMULATED',
        signal_id INTEGER, auto_trade INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")

    c.execute("""CREATE TABLE IF NOT EXISTS portfolio (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
        asset TEXT, platform TEXT, quantity REAL DEFAULT 0,
        avg_buy_price REAL DEFAULT 0, current_price REAL DEFAULT 0,
        pnl REAL DEFAULT 0, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")

    c.execute("""CREATE TABLE IF NOT EXISTS api_keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
        platform TEXT, api_key TEXT, api_secret TEXT,
        access_token TEXT, token_updated_at TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, platform))""")

    c.execute("""CREATE TABLE IF NOT EXISTS activity_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
        log_type TEXT, message TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")

    c.execute("""CREATE TABLE IF NOT EXISTS auto_trade_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
        asset TEXT, platform TEXT, signal TEXT, confidence INTEGER,
        suggested_qty REAL, entry_price REAL, target_price REAL,
        stop_loss REAL, risk_level TEXT, reasoning TEXT,
        status TEXT DEFAULT 'PENDING', approved INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")

    c.execute("""CREATE TABLE IF NOT EXISTS auto_trade_settings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
        platform TEXT, enabled INTEGER DEFAULT 0,
        max_trade_pct REAL DEFAULT 5.0, min_confidence INTEGER DEFAULT 75,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, platform))""")

    # ── Migrations for existing single-user DBs ───────────────────────────────
    for tbl, col in [
        ("signals","user_id INTEGER"),("trades","user_id INTEGER"),
        ("portfolio","user_id INTEGER"),("api_keys","user_id INTEGER"),
        ("activity_logs","user_id INTEGER"),("auto_trade_log","user_id INTEGER"),
        ("auto_trade_settings","user_id INTEGER"),
        ("api_keys","access_token TEXT"),("api_keys","token_updated_at TIMESTAMP"),
    ]:
        try: c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col}")
        except Exception: pass

    # Remove old UNIQUE constraint on api_keys.platform (now UNIQUE per user)
    # This is handled by the new table definition above for fresh installs

    conn.commit(); conn.close()

def ensure_user_defaults(user_id: int):
    """Create default API key rows and auto-trade settings for a new user."""
    conn = get_db()
    for plat in ["Upstox", "Groww", "CoinDCX"]:
        conn.execute(
            "INSERT OR IGNORE INTO api_keys (user_id, platform) VALUES (?,?)",
            (user_id, plat)
        )
        conn.execute(
            "INSERT OR IGNORE INTO auto_trade_settings (user_id, platform) VALUES (?,?)",
            (user_id, plat)
        )
    conn.commit(); conn.close()

def log_activity(message, log_type="INFO", user_id=None):
    conn = get_db()
    conn.execute("INSERT INTO activity_logs (user_id,log_type,message) VALUES (?,?,?)",
                 (user_id, log_type, message))
    conn.commit(); conn.close()

# ── PRICES ─────────────────────────────────────────────────────────────────────
def simulate_prices():
    while True:
        conn = get_db()
        for asset, base in BASE_PRICES.items():
            price  = base * (1 + random.uniform(-0.008, 0.008))
            change = ((price - base) / base) * 100
            conn.execute("INSERT INTO prices (asset,platform,price,change_pct) VALUES (?,?,?,?)",
                         (asset,PLATFORMS.get(asset,"Upstox"),round(price,2),round(change,4)))
        conn.commit(); conn.close()
        time.sleep(5)

def get_latest_prices():
    conn = get_db()
    rows = conn.execute("""SELECT p.asset,p.platform,p.price,p.change_pct,p.recorded_at FROM prices p
        INNER JOIN (SELECT asset,MAX(id) as max_id FROM prices GROUP BY asset) l ON p.id=l.max_id
        ORDER BY p.platform,p.asset""").fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ── GROWW API ──────────────────────────────────────────────────────────────────
def groww_checksum(secret, timestamp):
    sha = hashlib.sha256(); sha.update((secret+timestamp).encode("utf-8")); return sha.hexdigest()

def groww_token(api_key, api_secret):
    ts = str(int(time.time()))
    r  = http_requests.post("https://api.groww.in/v1/token/api/access",
         headers={"Authorization":f"Bearer {api_key}","Content-Type":"application/json"},
         json={"key_type":"approval","checksum":groww_checksum(api_secret,ts),"timestamp":ts},timeout=10)
    d = r.json()
    if d.get("token"): return d["token"]
    raise Exception(f"Groww token error: {d}")

def groww_hdr(token): return {"Authorization":f"Bearer {token}","Accept":"application/json","X-API-VERSION":"1.0"}

def fetch_groww_portfolio(api_key, api_secret):
    try:
        t=groww_token(api_key,api_secret); h=groww_hdr(t)

        # All DEMAT holdings come from one endpoint — stocks AND ETFs combined
        hr=http_requests.get("https://api.groww.in/v1/holdings/user",headers=h,timeout=10).json()
        all_holdings=[]
        if hr.get("status")=="SUCCESS":
            for x in hr.get("payload",{}).get("holdings",[]):
                all_holdings.append({
                    "platform":        "Groww",
                    "asset":           x.get("trading_symbol",""),
                    "isin":            x.get("isin",""),
                    "quantity":        x.get("quantity",0),
                    "avg_buy_price":   x.get("average_price",0),
                    "ltp":             x.get("ltp",0),
                    "pnl":             x.get("pnl",0),
                    "t1_quantity":     x.get("t1_quantity",0),
                    "free_quantity":   x.get("demat_free_quantity",0),
                    # instrument_type field: "EQ" for stocks, "ETF" for ETFs
                    "instrument_type": x.get("instrument_type","EQ"),
                    "exchange":        x.get("exchange","NSE"),
                    "type":            "HOLDING"
                })

        # Split ETFs from stocks based on instrument_type field from Groww API
        # Groww returns instrument_type = "ETF" for ETFs, "EQ" for equities
        # Also catch common ETF naming patterns as fallback (some ISINs start with INF)
        etf_isins_prefix = "INF"  # Mutual fund / ETF ISINs start with INF in India
        stocks = []
        etfs   = []
        for h in all_holdings:
            itype = str(h.get("instrument_type","")).upper()
            isin  = h.get("isin","")
            # Primary: use instrument_type field
            # Fallback: ISIN starting with INF = ETF/MF scheme (not a stock)
            if itype == "ETF" or (isin.startswith(etf_isins_prefix) and itype not in ("EQ","","NONE")):
                etfs.append(h)
            else:
                stocks.append(h)

        # Positions (Intraday / CNC)
        pr=http_requests.get("https://api.groww.in/v1/positions/user",headers=h,timeout=10).json()
        positions=[{
            "platform":"Groww","asset":x.get("trading_symbol",""),
            "quantity":x.get("quantity",0),"net_price":x.get("net_price",0),
            "realised_pnl":x.get("realised_pnl",0),"type":"POSITION"
        } for x in pr.get("payload",{}).get("positions",[]) if pr.get("status")=="SUCCESS"]

        # Mutual Funds — NOT available via Groww Trading API
        # The Trading API only covers DEMAT (equity + ETF) holdings
        # MF data requires Groww's separate investment platform (not exposed via API key)
        mf = []
        mf_note = "Groww Trading API does not expose Mutual Fund holdings. MF data is only available in the Groww app/website."

        return {
            "holdings":  all_holdings,
            "stocks":    stocks,
            "etfs":      etfs,
            "mf":        mf,
            "mf_note":   mf_note,
            "positions": positions,
            "status":    "SUCCESS",
            "total_holdings": len(all_holdings),
            "total_stocks":   len(stocks),
            "total_etfs":     len(etfs),
        }
    except Exception as e:
        return {"holdings":[],"stocks":[],"etfs":[],"mf":[],"positions":[],"status":"ERROR","error":str(e)}

def fetch_groww_trades(api_key, api_secret):
    try:
        t=groww_token(api_key,api_secret)
        r=http_requests.get("https://api.groww.in/v1/trades/user",headers=groww_hdr(t),timeout=10).json()
        return [{"platform":"Groww","asset":x.get("trading_symbol",""),"trade_type":x.get("transaction_type",""),
                 "quantity":float(x.get("quantity",0)),"price":float(x.get("price",0)),
                 "total_value":round(float(x.get("quantity",0))*float(x.get("price",0)),2),
                 "trade_id":x.get("groww_order_id",""),"status":"LIVE","created_at":x.get("trade_date","")}
                for x in r.get("payload",{}).get("trade_book",[]) if r.get("status")=="SUCCESS"]
    except Exception as e:
        return [{"platform":"Groww","error":str(e),"status":"ERROR"}]

def fetch_upstox_trades(access_token):
    try:
        import upstox_client
        cfg=upstox_client.Configuration(); cfg.access_token=access_token
        res=upstox_client.TradeApi(upstox_client.ApiClient(cfg)).get_trades_by_day("2dma")
        return [{"platform":"Upstox","asset":t.trading_symbol,"trade_type":t.transaction_type,
                 "quantity":t.quantity,"price":t.average_price,
                 "total_value":round(t.quantity*t.average_price,2),
                 "trade_id":t.order_id,"status":"LIVE","created_at":str(t.exchange_timestamp)} for t in res.data]
    except Exception as e:
        return [{"platform":"Upstox","error":str(e),"status":"ERROR"}]

def fetch_coindcx_trades(api_key, secret):
    try:
        body={"timestamp":int(round(time.time()*1000))}; jb=json.dumps(body,separators=(",",":"))
        sig=hmac.new(secret.encode(),jb.encode(),hashlib.sha256).hexdigest()
        r=http_requests.post("https://api.coindcx.com/exchange/v1/orders/trade_history",
            data=jb,headers={"Content-Type":"application/json","X-AUTH-APIKEY":api_key,"X-AUTH-SIGNATURE":sig},timeout=10).json()
        if not isinstance(r,list): return [{"platform":"CoinDCX","error":str(r),"status":"ERROR"}]
        return [{"platform":"CoinDCX","asset":t.get("symbol",""),"trade_type":t.get("side","").upper(),
                 "quantity":float(t.get("quantity",0)),"price":float(t.get("price",0)),
                 "total_value":round(float(t.get("quantity",0))*float(t.get("price",0)),2),
                 "trade_id":t.get("id",""),"status":"LIVE","created_at":str(t.get("timestamp",""))} for t in r]
    except Exception as e:
        return [{"platform":"CoinDCX","error":str(e),"status":"ERROR"}]

# ── COINDCX FULL PORTFOLIO (Exchange + Web3) ───────────────────────────────────
def coindcx_auth_headers(api_key, secret, body=None):
    if body is None: body = {"timestamp": int(round(time.time()*1000))}
    jb  = json.dumps(body, separators=(",",":"))
    sig = hmac.new(secret.encode(), jb.encode(), hashlib.sha256).hexdigest()
    return jb, {"Content-Type":"application/json","X-AUTH-APIKEY":api_key,"X-AUTH-SIGNATURE":sig}

def fetch_coindcx_live_prices():
    """Fetch live INR prices from CoinDCX public ticker (no auth needed)."""
    try:
        r = http_requests.get("https://api.coindcx.com/exchange/ticker", timeout=10).json()
        prices = {}
        if isinstance(r, list):
            for t in r:
                market = t.get("market","")
                # We want XXXINR pairs
                if market.endswith("INR"):
                    symbol = market[:-3].upper()  # strip INR suffix → currency code
                    prices[symbol] = float(t.get("last_price", 0) or 0)
        return prices
    except Exception:
        return {}

def fetch_coindcx_exchange_portfolio(api_key, secret):
    """Fetch CoinDCX Exchange balances with live INR valuations. Filters out dust (<₹1)."""
    try:
        jb, headers = coindcx_auth_headers(api_key, secret)
        r = http_requests.post("https://api.coindcx.com/exchange/v1/users/balances",
                               data=jb, headers=headers, timeout=10).json()
        if not isinstance(r, list):
            return []

        # Fetch live prices to calculate INR values
        live_prices = fetch_coindcx_live_prices()

        holdings = []
        for b in r:
            qty = float(b.get("balance","0") or 0)
            if qty <= 0: continue
            currency = b.get("currency","").upper()

            # Get live INR price
            inr_price = live_prices.get(currency, 0)

            # For INR itself
            if currency == "INR":
                inr_price = 1.0

            current_value = round(qty * inr_price, 2)

            # Skip dust — less than ₹1 value
            if current_value < 1.0 and currency != "INR":
                continue

            holdings.append({
                "asset":         currency,
                "quantity":      qty,
                "locked":        float(b.get("locked_balance","0") or 0),
                "inr_price":     inr_price,
                "current_value": current_value,
                "invested":      0,   # CoinDCX exchange API doesn't provide cost basis
                "pnl":           0,
                "pnl_pct":       0,
                "source":        "CoinDCX Exchange",
                "type":          "EXCHANGE"
            })

        # Sort by value descending
        holdings.sort(key=lambda x: x["current_value"], reverse=True)
        return holdings
    except Exception as e:
        return [{"asset":"ERROR","source":"CoinDCX Exchange","error":str(e),"type":"EXCHANGE","current_value":0,"quantity":0}]

def fetch_coindcx_web3_portfolio(api_key, secret):
    """
    CoinDCX Web3 wallet has no API — return seeded data from user's screenshots.
    These are the actual holdings as of 19-03-2026.
    """
    # Web3 wallet screenshot data — updated with live prices where possible
    live_prices = fetch_coindcx_live_prices()

    web3_holdings = [
        {"asset":"SOL",   "quantity":0.6082033,    "invested":0,      "source":"CoinDCX Web3","type":"WEB3"},
        {"asset":"NEET",  "quantity":2306.1488,     "invested":5567.10,"source":"CoinDCX Web3","type":"WEB3"},
        {"asset":"ETH",   "quantity":0.002402102,   "invested":482.94, "source":"CoinDCX Web3","type":"WEB3"},
        {"asset":"SIREN", "quantity":3.2393,         "invested":193.22, "source":"CoinDCX Web3","type":"WEB3"},
        {"asset":"USDC",  "quantity":0.00001,        "invested":0,      "source":"CoinDCX Web3","type":"WEB3"},
    ]

    # Screenshot fallback prices (used when live price not available)
    screenshot_prices = {"SOL":8468,"NEET":2.10,"ETH":202700,"SIREN":87.8,"USDC":84.0}

    tokens = []
    for h in web3_holdings:
        asset   = h["asset"]
        qty     = h["quantity"]
        price   = live_prices.get(asset, screenshot_prices.get(asset, 0))
        cur_val = round(qty * price, 2)
        inv     = h["invested"]
        pnl     = round(cur_val - inv, 2) if inv > 0 else 0
        pnl_pct = round((pnl / inv * 100) if inv > 0 else 0, 2)
        tokens.append({
            **h,
            "inr_price":     price,
            "current_value": cur_val,
            "pnl":           pnl,
            "pnl_pct":       pnl_pct,
        })
    return tokens

def fetch_full_coindcx_portfolio(api_key, secret):
    """Combines Exchange (live) + Web3 (seeded) portfolio."""
    exchange = fetch_coindcx_exchange_portfolio(api_key, secret)
    web3     = fetch_coindcx_web3_portfolio(api_key, secret)

    # Exchange totals (only tokens with value)
    exc_current  = sum(h.get("current_value",0) for h in exchange if "ERROR" not in h.get("asset",""))
    web3_current = sum(h.get("current_value",0) for h in web3)
    web3_invest  = sum(h.get("invested",0) for h in web3)
    web3_pnl     = sum(h.get("pnl",0) for h in web3)

    return {
        "exchange":          exchange,
        "web3":              web3,
        "exchange_total":    round(exc_current, 2),
        "web3_total":        round(web3_current, 2),
        "web3_invested":     round(web3_invest, 2),
        "web3_pnl":          round(web3_pnl, 2),
        "total_current":     round(exc_current + web3_current, 2),
        "total_invested":    round(web3_invest, 2),
        "total_pnl":         round(web3_pnl, 2),
        "fetched_at":        datetime.utcnow().isoformat() + "Z",
        "status":            "SUCCESS"
    }

# ── SCHEDULED PORTFOLIO CACHE ──────────────────────────────────────────────────
_coindcx_cache      = {"data": None, "fetched_at": None}
_coindcx_cache_lock = threading.Lock()
COINDCX_REFRESH_INTERVAL = 6 * 3600  # 6 hours in seconds

def refresh_coindcx_cache():
    """Background thread: refresh CoinDCX portfolio every 6 hours."""
    while True:
        conn = get_db()
        keys = {r["platform"]: dict(r) for r in conn.execute("SELECT * FROM api_keys").fetchall()}
        conn.close()
        c = keys.get("CoinDCX", {})
        if c.get("api_key") and c.get("api_secret"):
            try:
                data = fetch_full_coindcx_portfolio(c["api_key"], c["api_secret"])
                with _coindcx_cache_lock:
                    _coindcx_cache["data"]       = data
                    _coindcx_cache["fetched_at"] = time.time()
                log_activity("CoinDCX portfolio auto-refreshed (scheduled)", "INFO")
            except Exception as e:
                log_activity(f"CoinDCX auto-refresh failed: {e}", "ERROR")
        time.sleep(COINDCX_REFRESH_INTERVAL)

def get_coindcx_cached_or_fresh(api_key, secret, force=False):
    """Return cached data if fresh (<6h), else fetch fresh."""
    with _coindcx_cache_lock:
        cached_at = _coindcx_cache.get("fetched_at")
        if not force and cached_at and (time.time() - cached_at) < COINDCX_REFRESH_INTERVAL:
            data = _coindcx_cache["data"]
            if data:
                data["from_cache"] = True
                data["cache_age_minutes"] = round((time.time() - cached_at) / 60, 1)
                return data
    # Fetch fresh
    data = fetch_full_coindcx_portfolio(api_key, secret)
    with _coindcx_cache_lock:
        _coindcx_cache["data"]       = data
        _coindcx_cache["fetched_at"] = time.time()
    data["from_cache"] = False
    return data

# ── AUTO TRADE ENGINE ──────────────────────────────────────────────────────────
def run_auto_trade_analysis(user_id: int):
    try:
        prices=dict((r["asset"],r["price"]) for r in get_latest_prices())
        conn=get_db()
        settings={r["platform"]:dict(r) for r in conn.execute("SELECT * FROM auto_trade_settings WHERE user_id=?",(user_id,)).fetchall()}
        portfolio=conn.execute("SELECT * FROM portfolio WHERE user_id=? AND quantity>0",(user_id,)).fetchall()
        conn.close()
        holdings_ctx=[]
        for p in portfolio:
            p=dict(p); cp=prices.get(p["asset"],p["current_price"])
            pnl=((cp-p["avg_buy_price"])/p["avg_buy_price"]*100) if p["avg_buy_price"] else 0
            plat_settings=settings.get(p["platform"],{})
            if not plat_settings.get("enabled",0): continue
            holdings_ctx.append({"asset":p["asset"],"platform":p["platform"],"qty":p["quantity"],
                "avg_price":p["avg_buy_price"],"current_price":cp,"pnl_pct":round(pnl,2),
                "current_value":round(cp*p["quantity"],2)})
        if not holdings_ctx:
            return {"status":"no_enabled_platforms","decisions":[],"summary":"No platforms have auto-trading enabled."}
        gemini_rate_check()
        price_ctx=", ".join([f"{a}:₹{p:,.0f}" for a,p in list(prices.items())[:10]])
        prompt=f"""Current market prices: {price_ctx}\nPortfolio:\n{json.dumps(holdings_ctx,indent=2)}\nSettings:\n{json.dumps({k:{"min_confidence":v.get("min_confidence",75),"max_trade_pct":v.get("max_trade_pct",5)} for k,v in settings.items() if v.get("enabled")},indent=2)}\nAnalyse and provide decisions. Only action if confidence > min_confidence."""
        genai.configure(api_key=GEMINI_API_KEY)
        model=genai.GenerativeModel("gemini-2.5-flash",system_instruction=AUTO_TRADE_PROMPT)
        result=json.loads(model.generate_content(prompt).text.strip().replace("```json","").replace("```","").strip())
        conn=get_db()
        for d in result.get("decisions",[]):
            if d.get("signal") in ("BUY","SELL"):
                conn.execute("INSERT INTO auto_trade_log (user_id,asset,platform,signal,confidence,suggested_qty,entry_price,target_price,stop_loss,risk_level,reasoning,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,'PENDING')",
                    (user_id,d["asset"],d["platform"],d["signal"],d["confidence"],d.get("suggested_qty",0),d.get("entry_price",0),d.get("target_price",0),d.get("stop_loss",0),d.get("risk_level","MODERATE"),d.get("reasoning","")))
        conn.commit(); conn.close()
        log_activity(f"Auto-trade analysis: {result.get('total_actionable',0)} decisions pending","INFO",user_id)
        return result
    except Exception as e:
        log_activity(f"Auto-trade error: {str(e)}","ERROR",user_id)
        return {"status":"error","error":str(e),"decisions":[],"summary":"Analysis failed."}

def execute_approved_auto_trade(log_id: int, user_id: int):
    conn=get_db()
    row=conn.execute("SELECT * FROM auto_trade_log WHERE id=? AND user_id=?",(log_id,user_id)).fetchone()
    if not row: conn.close(); raise HTTPException(404,"Decision not found")
    row=dict(row)
    prices=dict((r["asset"],r["price"]) for r in get_latest_prices())
    price=prices.get(row["asset"],row["entry_price"]); total=round(price*row["suggested_qty"],2)
    conn.execute("INSERT INTO trades (user_id,asset,platform,trade_type,quantity,price,total_value,status,auto_trade) VALUES (?,?,?,?,?,?,?,?,?)",
        (user_id,row["asset"],row["platform"],row["signal"],row["suggested_qty"],price,total,"AUTO_EXECUTED",1))
    conn.execute("UPDATE auto_trade_log SET status='EXECUTED',approved=1 WHERE id=?",(log_id,))
    p=conn.execute("SELECT * FROM portfolio WHERE user_id=? AND asset=?",(user_id,row["asset"])).fetchone()
    if p:
        if row["signal"]=="BUY":
            nq=p["quantity"]+row["suggested_qty"]; na=((p["avg_buy_price"]*p["quantity"])+(price*row["suggested_qty"]))/nq
            conn.execute("UPDATE portfolio SET quantity=?,avg_buy_price=?,current_price=?,updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND asset=?",(nq,na,price,user_id,row["asset"]))
        else:
            conn.execute("UPDATE portfolio SET quantity=?,current_price=?,updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND asset=?",(max(0,p["quantity"]-row["suggested_qty"]),price,user_id,row["asset"]))
    conn.commit(); conn.close()
    log_activity(f"AUTO TRADE: {row['signal']} {row['suggested_qty']} {row['asset']} @ ₹{price:,.2f}","AUTO",user_id)
    return {"status":"executed","asset":row["asset"],"type":row["signal"],"qty":row["suggested_qty"],"price":price,"total":total}

# ── APP ─────────────────────────────────────────────────────────────────────────
app = FastAPI(title="ARTHA AI Trading System")

@app.on_event("startup")
def startup_event():
    print("🚀 Initialising ARTHA AI Trading System...")
    init_db()
    load_prices_from_db()   # populate BASE_PRICES from any previously synced holdings
    threading.Thread(target=simulate_prices,daemon=True).start()
    threading.Thread(target=refresh_coindcx_cache,daemon=True).start()
    print("✅ DB initialised | ✅ Price simulator running | ✅ CoinDCX scheduler running")
    print("🌐 http://localhost:8000 — Use 'Sync Portfolio' to fetch live holdings from brokers")

# ── PORTFOLIO SYNC ────────────────────────────────────────────────────────────
@app.post("/api/portfolio/sync")
def api_portfolio_sync(user=Depends(get_current_user)):
    uid=user["user_id"]; conn=get_db()
    keys={r["platform"]:dict(r) for r in conn.execute("SELECT * FROM api_keys WHERE user_id=?",(uid,)).fetchall()}
    conn.close()
    results={"synced":[],"skipped":[],"errors":[]}

    # ── Upstox ────────────────────────────────────────────────────────────────
    u = keys.get("Upstox", {})
    upstox_tok = u.get("access_token", "")
    if upstox_tok and upstox_tok != "CONNECTED":
        try:
            import upstox_client
            cfg = upstox_client.Configuration(); cfg.access_token = upstox_tok
            api = upstox_client.PortfolioApi(upstox_client.ApiClient(cfg))
            resp = api.get_holdings("2.0"); holdings = resp.data or []
            conn2 = get_db()
            conn2.execute("DELETE FROM portfolio WHERE user_id=? AND platform='Upstox'",(uid,))
            count = 0
            for h in holdings:
                sym=h.trading_symbol or ""; qty=h.quantity or 0; avg=h.average_price or 0; ltp=h.last_price or avg; pnl=h.pnl or 0
                if sym and qty>0:
                    conn2.execute("INSERT INTO portfolio (user_id,asset,platform,quantity,avg_buy_price,current_price,pnl,updated_at) VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                        (uid,sym,"Upstox",qty,avg,ltp,pnl))
                    BASE_PRICES[sym]=ltp if ltp>0 else avg; PLATFORMS[sym]="Upstox"; count+=1
            conn2.commit(); conn2.close()
            results["synced"].append(f"Upstox: {count} holdings")
            log_activity(f"Portfolio sync — Upstox: {count} holdings","INFO",uid)
        except Exception as e:
            err=str(e)
            if "401" in err or "unauthorized" in err.lower():
                results["errors"].append("Upstox: Token expired — re-login first")
            else:
                results["errors"].append(f"Upstox: {err}")
    else:
        results["skipped"].append("Upstox: No access token — complete OAuth login in API Keys first")

    # ── Groww ─────────────────────────────────────────────────────────────────
    g = keys.get("Groww", {})
    if g.get("api_key") and g.get("api_secret"):
        try:
            groww_tok=g.get("access_token",""); token_ok=False
            if groww_tok:
                try:
                    saved_at=datetime.fromisoformat((g.get("token_updated_at","") or "").replace("Z",""))
                    token_ok=(datetime.utcnow()-saved_at).total_seconds()<82800
                except Exception: pass
            if not token_ok:
                groww_tok=groww_token(g["api_key"],g["api_secret"])
                conn2=get_db()
                conn2.execute("UPDATE api_keys SET access_token=?,token_updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND platform='Groww'",(groww_tok,uid))
                conn2.commit(); conn2.close()
            h=groww_hdr(groww_tok)
            hr=http_requests.get("https://api.groww.in/v1/holdings/user",headers=h,timeout=10).json()
            groww_h=hr.get("payload",{}).get("holdings",[]) if hr.get("status")=="SUCCESS" else []
            conn2=get_db()
            conn2.execute("DELETE FROM portfolio WHERE user_id=? AND platform='Groww'",(uid,))
            count=0
            for x in groww_h:
                sym=x.get("trading_symbol",""); qty=x.get("quantity",0); avg=x.get("average_price",0); ltp=x.get("ltp",avg)
                if sym and qty>0:
                    conn2.execute("INSERT INTO portfolio (user_id,asset,platform,quantity,avg_buy_price,current_price,updated_at) VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                        (uid,sym,"Groww",qty,avg,ltp))
                    BASE_PRICES[sym]=ltp if ltp>0 else avg; PLATFORMS[sym]="Groww"; count+=1
            conn2.commit(); conn2.close()
            results["synced"].append(f"Groww: {count} holdings")
            log_activity(f"Portfolio sync — Groww: {count} holdings","INFO",uid)
        except Exception as e:
            results["errors"].append(f"Groww: {str(e)}")
    else:
        results["skipped"].append("Groww: No API keys — add them in API Keys first")

    # ── CoinDCX ───────────────────────────────────────────────────────────────
    c = keys.get("CoinDCX", {})
    if c.get("api_key") and c.get("api_secret"):
        try:
            exchange=fetch_coindcx_exchange_portfolio(c["api_key"],c["api_secret"])
            conn2=get_db()
            conn2.execute("DELETE FROM portfolio WHERE user_id=? AND platform='CoinDCX'",(uid,))
            count=0
            for h in exchange:
                if h.get("asset")=="ERROR": continue
                sym=h["asset"]+"/INR"; qty=h["quantity"]; price=h.get("inr_price",0)
                if qty>0 and price>0:
                    conn2.execute("INSERT INTO portfolio (user_id,asset,platform,quantity,avg_buy_price,current_price,updated_at) VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                        (uid,sym,"CoinDCX",qty,price,price))
                    BASE_PRICES[sym]=price; PLATFORMS[sym]="CoinDCX"; count+=1
            conn2.commit(); conn2.close()
            results["synced"].append(f"CoinDCX: {count} tokens")
            log_activity(f"Portfolio sync — CoinDCX: {count} tokens","INFO",uid)
        except Exception as e:
            results["errors"].append(f"CoinDCX: {str(e)}")
    else:
        results["skipped"].append("CoinDCX: No API keys configured")

    load_prices_from_db()   # refresh BASE_PRICES after sync
    results["status"]     = "SUCCESS" if results["synced"] else "PARTIAL"
    results["synced_at"]  = datetime.utcnow().isoformat() + "Z"
    return results

class AnalyzeRequest(BaseModel): asset: str
class TradeRequest(BaseModel):
    asset: str; trade_type: str; quantity: float; signal_id: int = None
class ApiKeyRequest(BaseModel):
    platform: str; api_key: str; api_secret: str = ""
class ChatRequest(BaseModel):
    message: str; history: list = []
class AutoTradeSettingsRequest(BaseModel):
    platform: str; enabled: bool; max_trade_pct: float = 5.0; min_confidence: int = 75
class ApproveTradeRequest(BaseModel):
    log_id: int

# ── AUTH MODELS ──────────────────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    name: str; email: str; password: str
class LoginRequest(BaseModel):
    email: str; password: str

# ── AUTH ENDPOINTS ───────────────────────────────────────────────────────────
@app.post("/api/auth/register")
def api_register(req: RegisterRequest):
    if len(req.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    if "@" not in req.email:
        raise HTTPException(400, "Invalid email address")
    if not req.name.strip():
        raise HTTPException(400, "Name is required")
    conn = get_db()
    existing = conn.execute("SELECT id FROM users WHERE email=?", (req.email.lower(),)).fetchone()
    if existing:
        conn.close(); raise HTTPException(400, "Email already registered")
    cur = conn.execute(
        "INSERT INTO users (email, password_hash, name) VALUES (?,?,?)",
        (req.email.lower(), hash_password(req.password), req.name.strip())
    )
    user_id = cur.lastrowid
    conn.commit(); conn.close()
    ensure_user_defaults(user_id)
    token = create_token(user_id, req.email.lower())
    log_activity(f"New user registered: {req.email}", "INFO", user_id)
    return {"token": token, "user": {"id": user_id, "email": req.email.lower(), "name": req.name}}

@app.post("/api/auth/login")
def api_login(req: LoginRequest):
    conn = get_db()
    user = conn.execute(
        "SELECT id, email, name, password_hash, role FROM users WHERE email=?",
        (req.email.lower(),)
    ).fetchone()
    if not user or user["password_hash"] != hash_password(req.password):
        conn.close(); raise HTTPException(401, "Invalid email or password")
    conn.execute("UPDATE users SET last_login=CURRENT_TIMESTAMP WHERE id=?", (user["id"],))
    conn.commit(); conn.close()
    token = create_token(user["id"], user["email"])
    log_activity(f"User logged in: {user['email']}", "INFO", user["id"])
    return {
        "token": token,
        "user": {"id": user["id"], "email": user["email"], "name": user["name"], "role": user["role"]}
    }

@app.get("/api/auth/me")
def api_me(user=Depends(get_current_user)):
    conn = get_db()
    u = conn.execute("SELECT id,email,name,role,created_at FROM users WHERE id=?", (user["user_id"],)).fetchone()
    conn.close()
    if not u: raise HTTPException(404, "User not found")
    return dict(u)

@app.post("/api/auth/change-password")
def api_change_password(req: dict, user=Depends(get_current_user)):
    old_pw = req.get("old_password","")
    new_pw = req.get("new_password","")
    if len(new_pw) < 6: raise HTTPException(400, "New password must be at least 6 characters")
    conn = get_db()
    u = conn.execute("SELECT password_hash FROM users WHERE id=?", (user["user_id"],)).fetchone()
    if not u or u["password_hash"] != hash_password(old_pw):
        conn.close(); raise HTTPException(401, "Current password is incorrect")
    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(new_pw), user["user_id"]))
    conn.commit(); conn.close()
    return {"status": "ok", "message": "Password changed successfully"}

# ── PRICES ──────────────────────────────────────────────────────────────────────
@app.get("/api/prices")
def api_prices(): return get_latest_prices()

# ── PORTFOLIO ───────────────────────────────────────────────────────────────────
@app.get("/api/portfolio")
def api_portfolio(user=Depends(get_current_user)):
    uid = user["user_id"]
    prices=dict((r["asset"],r["price"]) for r in get_latest_prices())
    conn=get_db(); rows=conn.execute("SELECT * FROM portfolio WHERE user_id=? AND quantity>0",(uid,)).fetchall(); conn.close()
    positions=[]; ps={"Upstox":{"invested":0,"current":0,"positions":[]},"Groww":{"invested":0,"current":0,"positions":[]},"CoinDCX":{"invested":0,"current":0,"positions":[]}}
    ti=tc=0
    for r in rows:
        r=dict(r); cp=prices.get(r["asset"],r["current_price"]); inv=r["avg_buy_price"]*r["quantity"]; cur=cp*r["quantity"]; pnl=cur-inv
        pos={**r,"current_price":cp,"pnl":round(pnl,2),"pnl_pct":round((pnl/inv*100) if inv else 0,2),"current_value":round(cur,2),"invested_value":round(inv,2)}
        positions.append(pos); ti+=inv; tc+=cur
        if r["platform"] in ps:
            ps[r["platform"]]["invested"]+=inv; ps[r["platform"]]["current"]+=cur; ps[r["platform"]]["positions"].append(pos)
    for p in ps:
        i=ps[p]["invested"]; c=ps[p]["current"]
        ps[p].update({"invested":round(i,2),"current":round(c,2),"pnl":round(c-i,2),"pnl_pct":round(((c-i)/i*100) if i else 0,2)})
    return {"positions":positions,"platform_summary":ps,"total_invested":round(ti,2),"total_current":round(tc,2),"total_pnl":round(tc-ti,2),"total_pnl_pct":round(((tc-ti)/ti*100) if ti else 0,2)}

@app.get("/api/portfolio/{platform}")
def api_portfolio_platform(platform: str):
    plat_map={"upstox":"Upstox","groww":"Groww","coindcx":"CoinDCX"}
    pk=plat_map.get(platform.lower())
    if not pk: raise HTTPException(400,"Unknown platform")
    full=api_portfolio(); s=full["platform_summary"].get(pk,{})
    return {"platform":pk,"positions":s.get("positions",[]),"invested":s.get("invested",0),"current":s.get("current",0),"pnl":s.get("pnl",0),"pnl_pct":s.get("pnl_pct",0)}

@app.get("/api/groww/portfolio")
def api_groww_live(user=Depends(get_current_user)):
    uid=user["user_id"]; conn=get_db()
    keys=dict((r["platform"],dict(r)) for r in conn.execute("SELECT * FROM api_keys WHERE user_id=?",(uid,)).fetchall()); conn.close()
    g=keys.get("Groww",{})
    api_key    = g.get("api_key","")
    api_secret = g.get("api_secret","")
    access_token = g.get("access_token","")

    if not api_key or not api_secret:
        return {"holdings":[],"positions":[],"status":"NOT_CONFIGURED",
                "error":"Groww API Key and Secret not saved. Add them in API Keys."}

    # Check token age — if expired or missing, try to auto-regenerate
    token_ok = False
    if access_token:
        try:
            saved_at = datetime.fromisoformat((g.get("token_updated_at","") or "").replace("Z",""))
            age_secs = (datetime.utcnow() - saved_at).total_seconds()
            token_ok = age_secs < 82800
        except Exception:
            token_ok = False

    if not token_ok:
        # Try to regenerate token automatically
        try:
            ts       = str(int(time.time()))
            checksum = groww_checksum(api_secret, ts)
            resp     = http_requests.post(
                "https://api.groww.in/v1/token/api/access",
                headers={"Authorization":f"Bearer {api_key}","Content-Type":"application/json"},
                json={"key_type":"approval","checksum":checksum,"timestamp":ts}, timeout=10
            ).json()
            if resp.get("token"):
                access_token = resp["token"]
                conn2 = get_db()
                conn2.execute("UPDATE api_keys SET access_token=?, token_updated_at=CURRENT_TIMESTAMP WHERE platform=?",
                              (access_token, "Groww"))
                conn2.commit(); conn2.close()
                log_activity("Groww token auto-regenerated on portfolio fetch","INFO")
            else:
                msg = resp.get("message") or json.dumps(resp)
                return {"holdings":[],"positions":[],"status":"APPROVAL_PENDING",
                        "error":f"Groww token needs approval. Open Groww app → Profile → Trading APIs → Approve session, then click Generate Token. ({msg})"}
        except Exception as e:
            return {"holdings":[],"positions":[],"status":"ERROR","error":str(e)}

    # Use the valid token to fetch portfolio
    try:
        h = groww_hdr(access_token)
        hr = http_requests.get("https://api.groww.in/v1/holdings/user", headers=h, timeout=10).json()
        holdings=[{"platform":"Groww","asset":x.get("trading_symbol",""),"isin":x.get("isin",""),
                   "quantity":x.get("quantity",0),"avg_buy_price":x.get("average_price",0),
                   "t1_quantity":x.get("t1_quantity",0),"free_quantity":x.get("demat_free_quantity",0),"type":"HOLDING"}
                  for x in hr.get("payload",{}).get("holdings",[]) if hr.get("status")=="SUCCESS"]
        pr = http_requests.get("https://api.groww.in/v1/positions/user", headers=h, timeout=10).json()
        positions=[{"platform":"Groww","asset":x.get("trading_symbol",""),"quantity":x.get("quantity",0),
                    "net_price":x.get("net_price",0),"realised_pnl":x.get("realised_pnl",0),"type":"POSITION"}
                   for x in pr.get("payload",{}).get("positions",[]) if pr.get("status")=="SUCCESS"]
        log_activity(f"Groww portfolio fetched: {len(holdings)} holdings, {len(positions)} positions","INFO")
        return {"holdings":holdings,"positions":positions,"status":"SUCCESS",
                "fetched_at":datetime.utcnow().isoformat()+"Z"}
    except Exception as e:
        return {"holdings":[],"positions":[],"status":"ERROR","error":str(e)}

# ── UPSTOX LIVE PORTFOLIO ─────────────────────────────────────────────────────
@app.get("/api/upstox/portfolio")
def api_upstox_live(user=Depends(get_current_user)):
    uid=user["user_id"]; conn=get_db()
    keys=dict((r["platform"],dict(r)) for r in conn.execute("SELECT * FROM api_keys WHERE user_id=?",(uid,)).fetchall()); conn.close()
    u=keys.get("Upstox",{})
    access_token = u.get("access_token","")  # Always read from dedicated access_token column

    if not access_token:
        return {
            "holdings":[], "positions":[], "status":"NOT_CONFIGURED",
            "error": "No access token found. Go to API Keys → save your API Key + Secret → click 'Login with Upstox'.",
            "action": "login",
            "fetched_at": datetime.utcnow().isoformat()+"Z"
        }

    # Check token age before even trying — saves a round-trip on obvious expiry
    conn2=get_db(); row=conn2.execute("SELECT token_updated_at FROM api_keys WHERE platform='Upstox'").fetchone(); conn2.close()
    if row and row["token_updated_at"]:
        try:
            saved_at  = datetime.fromisoformat(row["token_updated_at"].replace("Z",""))
            age_hours = (datetime.utcnow() - saved_at).total_seconds() / 3600
            if age_hours > 23:
                return {
                    "holdings":[], "positions":[], "status":"TOKEN_EXPIRED",
                    "error": f"Token expired ({age_hours:.1f}h old). Click 'Re-login Upstox' to get a fresh token — takes 10 seconds.",
                    "action": "login",
                    "token_age_hours": round(age_hours,1),
                    "fetched_at": datetime.utcnow().isoformat()+"Z"
                }
        except Exception:
            pass
    try:
        import upstox_client
        config=upstox_client.Configuration(); config.access_token=access_token
        client=upstox_client.ApiClient(config)
        portfolio_api=upstox_client.PortfolioApi(client)

        holdings_resp=portfolio_api.get_holdings("2.0")
        holdings=[]
        for h in (holdings_resp.data or []):
            holdings.append({
                "trading_symbol":        h.trading_symbol or h.tradingsymbol or "",
                "isin":                  h.isin or "",
                "quantity":              h.quantity or 0,
                "average_price":         h.average_price or 0,
                "last_price":            h.last_price or 0,
                "close_price":           h.close_price or 0,
                "pnl":                   h.pnl or 0,
                "day_change":            h.day_change or 0,
                "day_change_percentage": h.day_change_percentage or 0,
                "exchange":              h.exchange or "",
                "t1_quantity":           h.t1_quantity or 0,
            })

        positions_resp=portfolio_api.get_positions("2.0")
        positions=[]
        for p in (positions_resp.data or []):
            positions.append({
                "trading_symbol": p.trading_symbol or p.tradingsymbol or "",
                "exchange":       p.exchange or "",
                "quantity":       p.quantity or 0,
                "average_price":  p.average_price or 0,
                "last_price":     p.last_price or 0,
                "pnl":            p.pnl or 0,
                "realised":       p.realised or 0,
                "unrealised":     p.unrealised or 0,
                "product":        p.product or "",
            })

        token_age = ""
        if u.get("token_updated_at"):
            token_age = u["token_updated_at"]

        log_activity(f"Upstox live data: {len(holdings)} holdings, {len(positions)} positions","INFO")
        return {
            "holdings":        holdings,
            "positions":       positions,
            "total_holdings":  len(holdings),
            "total_positions": len(positions),
            "token_updated_at":token_age,
            "fetched_at":      datetime.utcnow().isoformat()+"Z",
            "status":          "SUCCESS"
        }
    except Exception as e:
        err=str(e)
        if "401" in err or "unauthorized" in err.lower():
            msg="Token expired. Click 'Login with Upstox' on the API Keys page to get a fresh token."
        elif "403" in err:
            msg="Token doesn't have portfolio permissions. Regenerate with correct scopes."
        else:
            msg=err
        log_activity(f"Upstox fetch error: {msg}","ERROR")
        return {"holdings":[],"positions":[],"status":"ERROR","error":msg,"fetched_at":datetime.utcnow().isoformat()+"Z"}

# ── AI SIGNAL ───────────────────────────────────────────────────────────────────
@app.post("/api/analyze")
def api_analyze(req: AnalyzeRequest, user=Depends(get_current_user)):
    uid=user["user_id"]; asset=req.asset
    if asset not in BASE_PRICES: raise HTTPException(400,"Unknown asset")
    gemini_rate_check()
    prices=dict((r["asset"],r["price"]) for r in get_latest_prices())
    price=prices.get(asset,BASE_PRICES[asset]); change=((price-BASE_PRICES[asset])/BASE_PRICES[asset])*100; platform=PLATFORMS.get(asset,"Upstox")
    prompt=f"Analyze for moderate-risk Indian investor:\nPlatform:{platform} Asset:{asset}\nPrice:₹{price:,.2f} Change:{change:.2f}%\nMarket:{'Crypto' if platform=='CoinDCX' else 'Indian Stock'}\nGive entry,target,stop-loss. Prefer LOW risk."
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        result=json.loads(genai.GenerativeModel("gemini-2.5-flash",system_instruction=SYSTEM_PROMPT).generate_content(prompt).text.strip().replace("```json","").replace("```","").strip())
    except:
        result={"signal":"HOLD","confidence":55,"entry_price":price,"target_price":round(price*1.03,2),"stop_loss":round(price*0.97,2),"reasoning":"Defaulting to HOLD.","risk_level":"LOW","strategy":"Wait and Watch","time_horizon":"1-3 days"}
    conn=get_db()
    cursor=conn.execute("INSERT INTO signals (user_id,asset,platform,signal,confidence,entry_price,target_price,stop_loss,reasoning,risk_level,strategy,time_horizon,price_at_signal) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uid,asset,platform,result["signal"],result["confidence"],result.get("entry_price"),result.get("target_price"),result.get("stop_loss"),result.get("reasoning"),result.get("risk_level"),result.get("strategy"),result.get("time_horizon"),price))
    conn.commit(); conn.close()
    log_activity(f"Signal for {asset}: {result['signal']} ({result['confidence']}%)",result["signal"],uid)
    return {**result,"id":cursor.lastrowid,"asset":asset,"platform":platform,"price":price}

@app.get("/api/signals")
def api_signals(limit: int=20, user=Depends(get_current_user)):
    conn=get_db(); rows=conn.execute("SELECT * FROM signals WHERE user_id=? ORDER BY created_at DESC LIMIT ?",(user["user_id"],limit)).fetchall(); conn.close()
    return [dict(r) for r in rows]

# ── MANUAL TRADE ────────────────────────────────────────────────────────────────
@app.post("/api/trade")
def api_trade(req: TradeRequest, user=Depends(get_current_user)):
    uid=user["user_id"]
    prices=dict((r["asset"],r["price"]) for r in get_latest_prices())
    price=prices.get(req.asset,BASE_PRICES.get(req.asset,0)); total=round(price*req.quantity,2); platform=PLATFORMS.get(req.asset,"Upstox")
    conn=get_db()
    conn.execute("INSERT INTO trades (user_id,asset,platform,trade_type,quantity,price,total_value,signal_id) VALUES (?,?,?,?,?,?,?,?)",
        (uid,req.asset,platform,req.trade_type,req.quantity,price,total,req.signal_id))
    row=conn.execute("SELECT * FROM portfolio WHERE user_id=? AND asset=?",(uid,req.asset)).fetchone()
    if row:
        if req.trade_type=="BUY":
            nq=row["quantity"]+req.quantity; na=((row["avg_buy_price"]*row["quantity"])+(price*req.quantity))/nq
            conn.execute("UPDATE portfolio SET quantity=?,avg_buy_price=?,current_price=?,updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND asset=?",(nq,na,price,uid,req.asset))
        else:
            conn.execute("UPDATE portfolio SET quantity=?,current_price=?,updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND asset=?",(max(0,row["quantity"]-req.quantity),price,uid,req.asset))
    else:
        conn.execute("INSERT INTO portfolio (user_id,asset,platform,quantity,avg_buy_price,current_price) VALUES (?,?,?,?,?,?)",(uid,req.asset,platform,req.quantity,price,price))
    conn.commit(); conn.close()
    log_activity(f"Trade:{req.trade_type} {req.quantity} {req.asset} @ ₹{price:,.2f}=₹{total:,.2f}",req.trade_type,uid)
    return {"status":"SIMULATED","asset":req.asset,"type":req.trade_type,"qty":req.quantity,"price":price,"total":total}

# ── AUTO TRADE ENDPOINTS ─────────────────────────────────────────────────────────
@app.post("/api/auto-trade/analyze")
def api_auto_analyze(user=Depends(get_current_user)):
    return run_auto_trade_analysis(user["user_id"])

@app.get("/api/auto-trade/pending")
def api_auto_pending(user=Depends(get_current_user)):
    conn=get_db()
    rows=conn.execute("SELECT * FROM auto_trade_log WHERE user_id=? AND status='PENDING' ORDER BY created_at DESC",(user["user_id"],)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/api/auto-trade/history")
def api_auto_history(user=Depends(get_current_user)):
    conn=get_db()
    rows=conn.execute("SELECT * FROM auto_trade_log WHERE user_id=? ORDER BY created_at DESC LIMIT 100",(user["user_id"],)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/auto-trade/approve")
def api_auto_approve(req: ApproveTradeRequest, user=Depends(get_current_user)):
    return execute_approved_auto_trade(req.log_id, user["user_id"])

@app.post("/api/auto-trade/reject/{log_id}")
def api_auto_reject(log_id: int, user=Depends(get_current_user)):
    conn=get_db()
    conn.execute("UPDATE auto_trade_log SET status='REJECTED',approved=0 WHERE id=? AND user_id=?",(log_id,user["user_id"]))
    conn.commit(); conn.close()
    log_activity(f"Auto-trade #{log_id} rejected","INFO",user["user_id"])
    return {"status":"rejected","log_id":log_id}

@app.post("/api/auto-trade/reject-all")
def api_auto_reject_all(user=Depends(get_current_user)):
    conn=get_db()
    conn.execute("UPDATE auto_trade_log SET status='REJECTED' WHERE status='PENDING' AND user_id=?",(user["user_id"],))
    conn.commit(); conn.close()
    return {"status":"all_rejected"}

@app.get("/api/auto-trade/settings")
def api_auto_settings(user=Depends(get_current_user)):
    conn=get_db(); rows=conn.execute("SELECT * FROM auto_trade_settings WHERE user_id=?",(user["user_id"],)).fetchall(); conn.close()
    return [dict(r) for r in rows]

@app.post("/api/auto-trade/settings")
def api_save_auto_settings(req: AutoTradeSettingsRequest, user=Depends(get_current_user)):
    uid=user["user_id"]; conn=get_db()
    conn.execute("UPDATE auto_trade_settings SET enabled=?,max_trade_pct=?,min_confidence=?,updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND platform=?",
                 (1 if req.enabled else 0,req.max_trade_pct,req.min_confidence,uid,req.platform))
    conn.commit(); conn.close()
    log_activity(f"Auto-trading {'ON' if req.enabled else 'OFF'} for {req.platform}","INFO",uid)
    return {"status":"saved"}

# ── TRADES ──────────────────────────────────────────────────────────────────────
@app.get("/api/trades")
def api_trades(limit: int=200, user=Depends(get_current_user)):
    conn=get_db(); rows=conn.execute("SELECT * FROM trades WHERE user_id=? ORDER BY created_at DESC LIMIT ?",(user["user_id"],limit)).fetchall(); conn.close()
    return [dict(r) for r in rows]

@app.get("/api/trades/{platform}")
def api_trades_platform(platform: str, limit: int=200, user=Depends(get_current_user)):
    plat_map={"upstox":"Upstox","groww":"Groww","coindcx":"CoinDCX"}
    pk=plat_map.get(platform.lower())
    if not pk: raise HTTPException(400,"Unknown platform")
    conn=get_db(); rows=conn.execute("SELECT * FROM trades WHERE user_id=? AND platform=? ORDER BY created_at DESC LIMIT ?",(user["user_id"],pk,limit)).fetchall(); conn.close()
    return [dict(r) for r in rows]

@app.get("/api/real-trades")
def api_real_trades(user=Depends(get_current_user)):
    uid=user["user_id"]; conn=get_db()
    keys=dict((r["platform"],dict(r)) for r in conn.execute("SELECT * FROM api_keys WHERE user_id=?",(uid,)).fetchall()); conn.close()
    all_trades=[]
    uk=keys.get("Upstox",{}).get("access_token","")
    all_trades+=(fetch_upstox_trades(uk) if uk else [{"platform":"Upstox","note":"Login required","status":"NOT_CONFIGURED"}])
    g=keys.get("Groww",{})
    all_trades+=(fetch_groww_trades(g["api_key"],g["api_secret"]) if g.get("api_key") and g.get("api_secret") else [{"platform":"Groww","note":"No API key","status":"NOT_CONFIGURED"}])
    c=keys.get("CoinDCX",{})
    all_trades+=(fetch_coindcx_trades(c["api_key"],c["api_secret"]) if c.get("api_key") and c.get("api_secret") else [{"platform":"CoinDCX","note":"No API key","status":"NOT_CONFIGURED"}])
    return {"trades":all_trades,"total":len([t for t in all_trades if t.get("trade_id")])}

# ── COINDCX PORTFOLIO (Exchange + Web3) ─────────────────────────────────────────
@app.get("/api/coindcx/portfolio")
def api_coindcx_portfolio(force: bool=False, user=Depends(get_current_user)):
    uid=user["user_id"]; conn=get_db()
    keys={r["platform"]:dict(r) for r in conn.execute("SELECT * FROM api_keys WHERE user_id=?",(uid,)).fetchall()}; conn.close()
    c=keys.get("CoinDCX",{})
    if not c.get("api_key") or not c.get("api_secret"):
        # Seed data from screenshots with live price calculation attempted
        live = fetch_coindcx_live_prices()
        sp   = {"SOL":8468,"NEET":2.10,"ETH":202700,"SIREN":87.8,"USDC":84.0,"ZEC":21976}
        web3 = [
            {"asset":"SOL",  "quantity":0.6082033,  "invested":0,      "type":"WEB3","source":"CoinDCX Web3"},
            {"asset":"NEET", "quantity":2306.1488,   "invested":5567.10,"type":"WEB3","source":"CoinDCX Web3"},
            {"asset":"ETH",  "quantity":0.002402102, "invested":482.94, "type":"WEB3","source":"CoinDCX Web3"},
            {"asset":"SIREN","quantity":3.2393,       "invested":193.22, "type":"WEB3","source":"CoinDCX Web3"},
        ]
        exchange = [
            {"asset":"ZEC","quantity":0.15445,"locked":0,"type":"EXCHANGE","source":"CoinDCX Exchange","invested":3089.0},
        ]
        for h in web3+exchange:
            p=live.get(h["asset"],sp.get(h["asset"],0))
            h["inr_price"]=p; h["current_value"]=round(h["quantity"]*p,2)
            h["pnl"]=round(h["current_value"]-h["invested"],2) if h["invested"]>0 else 0
            h["pnl_pct"]=round((h["pnl"]/h["invested"]*100) if h["invested"]>0 else 0,2)
        w3t=sum(h["current_value"] for h in web3)
        ext=sum(h["current_value"] for h in exchange)
        w3i=sum(h["invested"] for h in web3)
        w3p=sum(h["pnl"] for h in web3)
        return {
            "exchange": exchange, "web3": web3,
            "exchange_total": round(ext,2), "web3_total": round(w3t,2),
            "web3_invested": round(w3i,2), "web3_pnl": round(w3p,2),
            "total_current": round(ext+w3t,2), "total_invested": round(w3i,2),
            "total_pnl": round(w3p,2),
            "fetched_at": datetime.utcnow().isoformat()+"Z",
            "status": "SEEDED", "note": "Configure CoinDCX API keys for live exchange data"
        }
    return get_coindcx_cached_or_fresh(c["api_key"], c["api_secret"], force=force)

@app.post("/api/coindcx/portfolio/refresh")
def api_coindcx_force_refresh():
    """Manual force-refresh of CoinDCX portfolio — ignores 6h cache."""
    conn = get_db()
    keys = {r["platform"]: dict(r) for r in conn.execute("SELECT * FROM api_keys").fetchall()}
    conn.close()
    c = keys.get("CoinDCX", {})
    if not c.get("api_key") or not c.get("api_secret"):
        raise HTTPException(400, "CoinDCX API key/secret not configured in API Keys settings.")
    data = get_coindcx_cached_or_fresh(c["api_key"], c["api_secret"], force=True)
    log_activity("CoinDCX portfolio manually refreshed by user", "INFO")
    return data

@app.get("/api/coindcx/cache-status")
def api_coindcx_cache_status():
    """Returns when the CoinDCX portfolio was last fetched and when it will auto-refresh."""
    with _coindcx_cache_lock:
        fetched_at = _coindcx_cache.get("fetched_at")
    if not fetched_at:
        return {"cached": False, "next_refresh_in_hours": 0, "last_fetched": None}
    age_seconds   = time.time() - fetched_at
    remaining_sec = max(0, COINDCX_REFRESH_INTERVAL - age_seconds)
    return {
        "cached":                True,
        "last_fetched":          datetime.utcfromtimestamp(fetched_at).isoformat() + "Z",
        "age_minutes":           round(age_seconds / 60, 1),
        "next_refresh_in_hours": round(remaining_sec / 3600, 2),
        "next_refresh_in_minutes": round(remaining_sec / 60, 1),
        "refresh_interval_hours": COINDCX_REFRESH_INTERVAL / 3600
    }

# ── CHAT ────────────────────────────────────────────────────────────────────────
@app.post("/api/chat")
def api_chat(req: ChatRequest, user=Depends(get_current_user)):
    gemini_rate_check()
    prices=dict((r["asset"],r["price"]) for r in get_latest_prices())
    ctx=", ".join([f"{a}:₹{p:,.0f}" for a,p in list(prices.items())[:8]])
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        reply=genai.GenerativeModel("gemini-2.5-flash",system_instruction=f"You are ARTHA AI, expert trading assistant for Indian markets. Prices:{ctx}. Risk:Moderate. Be concise, mention risk.").generate_content(req.message).text
    except Exception as e: reply=f"Error:{e}"
    return {"reply":reply}

# ── KEYS ────────────────────────────────────────────────────────────────────────
@app.post("/api/keys")
def api_save_keys(req: ApiKeyRequest, user=Depends(get_current_user)):
    uid=user["user_id"]; conn=get_db()
    conn.execute(
        "UPDATE api_keys SET api_key=?,api_secret=?,updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND platform=?",
        (req.api_key, req.api_secret, uid, req.platform)
    )
    conn.commit(); conn.close()
    log_activity(f"API key updated for {req.platform}","INFO",uid)
    return {"status":"saved"}

@app.get("/api/keys")
def api_get_keys(user=Depends(get_current_user)):
    uid=user["user_id"]; conn=get_db()
    rows=conn.execute("SELECT platform, api_key, api_secret, access_token, token_updated_at FROM api_keys WHERE user_id=?",(uid,)).fetchall()
    conn.close()
    result=[]
    for r in rows:
        d=dict(r); token=d.get("access_token","") or ""
        d["has_access_token"]=(bool(token) and token!="CONNECTED")
        d["token_preview"]=(token[:16]+"...") if len(token)>20 else token
        d.pop("access_token",None)
        result.append(d)
    return result

@app.get("/api/keys/token-status")
def api_token_status(user=Depends(get_current_user)):
    uid=user["user_id"]; conn=get_db()
    rows=conn.execute("SELECT platform, api_key, api_secret, access_token, token_updated_at FROM api_keys WHERE user_id=?",(uid,)).fetchall()
    conn.close()
    status={}
    for r in rows:
        d=dict(r); plat=d["platform"]
        has_keys=bool(d.get("api_key")) and bool(d.get("api_secret"))
        has_token=bool(d.get("access_token"))
        token_expired=False; token_age_hours=None
        if has_token and d.get("token_updated_at"):
            try:
                saved_at=datetime.fromisoformat(d["token_updated_at"].replace("Z",""))
                age_secs=(datetime.utcnow()-saved_at).total_seconds()
                token_age_hours=round(age_secs/3600,1)
                if plat in ("Upstox","Groww") and age_secs>82800: token_expired=True
            except Exception: pass
        status[plat]={
            "has_api_key":bool(d.get("api_key")),"has_api_secret":bool(d.get("api_secret")),
            "has_access_token":has_token,"token_expired":token_expired,
            "token_age_hours":token_age_hours,"token_updated_at":d.get("token_updated_at",""),
            "ready":(has_token and not token_expired) if plat in ("Upstox","Groww") else has_keys,
        }
    return status

# ── GROWW TOKEN GENERATION ────────────────────────────────────────────────────
@app.post("/api/groww/generate-token")
def api_groww_generate_token(user=Depends(get_current_user)):
    uid=user["user_id"]; conn=get_db()
    keys={r["platform"]:dict(r) for r in conn.execute("SELECT * FROM api_keys WHERE user_id=?",(uid,)).fetchall()}
    conn.close()
    g=keys.get("Groww",{}); api_key=g.get("api_key",""); api_secret=g.get("api_secret","")
    if not api_key or not api_secret:
        return {"status":"ERROR","error":"Groww API Key and Secret not saved. Add them in API Keys first."}
    try:
        ts=str(int(time.time())); checksum=groww_checksum(api_secret,ts)
        resp=http_requests.post("https://api.groww.in/v1/token/api/access",
            headers={"Authorization":f"Bearer {api_key}","Content-Type":"application/json"},
            json={"key_type":"approval","checksum":checksum,"timestamp":ts},timeout=10)
        d=resp.json()
        if not d.get("token"):
            msg=d.get("message") or d.get("error") or json.dumps(d)
            if "approval" in str(msg).lower() or "pending" in str(msg).lower() or "GRAPI" in str(msg):
                return {"status":"APPROVAL_PENDING","error":"Session approval required. Open Groww app → Profile → Trading APIs → Approve pending session, then Generate Token."}
            return {"status":"ERROR","error":msg}
        token=d["token"]
        conn2=get_db()
        conn2.execute("UPDATE api_keys SET access_token=?,token_updated_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND platform=?",
            (token,uid,"Groww"))
        conn2.commit(); conn2.close()
        log_activity("Groww token generated","INFO",uid)
        return {"status":"SUCCESS","message":"Groww token saved. Valid until 6 AM IST tomorrow.","expiry":d.get("expiry",""),"session":d.get("sessionName","")}
    except Exception as e:
        return {"status":"ERROR","error":str(e)}

# ── COINDCX CONNECTION TEST ───────────────────────────────────────────────────
@app.post("/api/coindcx/test-connection")
def api_coindcx_test(user=Depends(get_current_user)):
    uid=user["user_id"]; conn=get_db()
    keys={r["platform"]:dict(r) for r in conn.execute("SELECT * FROM api_keys WHERE user_id=?",(uid,)).fetchall()}
    conn.close()
    c=keys.get("CoinDCX",{}); api_key=c.get("api_key",""); secret=c.get("api_secret","")
    if not api_key or not secret:
        return {"status":"ERROR","error":"CoinDCX API Key and Secret not saved."}
    try:
        body={"timestamp":int(round(time.time()*1000))}; jb=json.dumps(body,separators=(",",":"))
        sig=hmac.new(secret.encode(),jb.encode(),hashlib.sha256).hexdigest()
        r=http_requests.post("https://api.coindcx.com/exchange/v1/users/balances",data=jb,
            headers={"Content-Type":"application/json","X-AUTH-APIKEY":api_key,"X-AUTH-SIGNATURE":sig},timeout=10).json()
        if isinstance(r,list):
            non_zero=[b for b in r if float(b.get("balance","0") or 0)>0]
            conn2=get_db()
            conn2.execute("UPDATE api_keys SET access_token=?,token_updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND platform=?",
                ("CONNECTED",uid,"CoinDCX"))
            conn2.commit(); conn2.close()
            log_activity(f"CoinDCX connected — {len(non_zero)} tokens found","INFO",uid)
            return {"status":"SUCCESS","message":f"CoinDCX connected. Found {len(non_zero)} tokens with balance.","token_count":len(non_zero)}
        else:
            return {"status":"ERROR","error":r.get("message") or json.dumps(r)}
    except Exception as e:
        return {"status":"ERROR","error":str(e)}

# ── GEMINI USAGE ────────────────────────────────────────────────────────────────
@app.get("/api/gemini/usage")
def api_gemini_usage():
    return gemini_usage()

# ── LOGS ────────────────────────────────────────────────────────────────────────
@app.get("/api/logs")
def api_logs(limit: int=100, user=Depends(get_current_user)):
    conn=get_db(); rows=conn.execute("SELECT * FROM activity_logs WHERE user_id=? ORDER BY created_at DESC LIMIT ?",(user["user_id"],limit)).fetchall(); conn.close()
    return [dict(r) for r in rows]

@app.delete("/api/logs")
def api_clear_logs(user=Depends(get_current_user)):
    conn=get_db(); conn.execute("DELETE FROM activity_logs WHERE user_id=?",(user["user_id"],)); conn.commit(); conn.close()
    return {"status":"cleared"}

# ── UPSTOX OAUTH FLOW ─────────────────────────────────────────────────────────
UPSTOX_REDIRECT_URI = "http://127.0.0.1:8000/upstox/callback"
UPSTOX_AUTH_URL     = "https://api.upstox.com/v2/login/authorization/dialog"
UPSTOX_TOKEN_URL    = "https://api.upstox.com/v2/login/authorization/token"

@app.get("/upstox/login")
def upstox_login(token: str = None, user=Depends(get_current_user_optional)):
    """
    Redirects user to official Upstox OAuth login page.
    Accepts JWT either via Authorization header OR ?token= query param
    (query param needed because browser popups can't send custom headers).
    """
    # Resolve user from header OR query param token
    resolved_user = user
    if not resolved_user and token:
        try:
            resolved_user = decode_token(token)
        except Exception:
            pass

    if not resolved_user:
        return HTMLResponse(content=_oauth_error_page(
            "Not Authenticated",
            "Your session could not be verified. Please log in to ARTHA first, then try again."
        ), status_code=401)

    uid = resolved_user["user_id"]
    conn = get_db()
    keys = {r["platform"]:dict(r) for r in conn.execute("SELECT * FROM api_keys WHERE user_id=?",(uid,)).fetchall()}
    conn.close()
    api_key = keys.get("Upstox",{}).get("api_key","")

    if not api_key:
        return HTMLResponse(content=_oauth_error_page(
            "Upstox API Key not saved",
            "Please go to API Keys, save your Upstox API Key (Client ID) and Secret, then try Login with Upstox again."
        ), status_code=400)

    if api_key.startswith("eyJ"):
        return HTMLResponse(content=_oauth_error_page(
            "Wrong value in API Key field",
            "Your API Key field contains a token instead of your Client ID. Go to API Keys → paste your actual Upstox Client ID → Save Keys → then Login."
        ), status_code=400)

    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id":     api_key,
        "redirect_uri":  UPSTOX_REDIRECT_URI,
        "state":         str(uid),  # user_id passed back in callback
    })
    return RedirectResponse(url=f"{UPSTOX_AUTH_URL}?{params}")

@app.get("/upstox/callback")
def upstox_callback(request: Request, code: str = None, error: str = None, state: str = None):
    if error:
        return HTMLResponse(content=_oauth_error_page("Login Failed",f"Upstox returned: {error}"),status_code=400)
    if not code:
        return HTMLResponse(content=_oauth_error_page("No Code Received","Upstox did not return an auth code."),status_code=400)

    # Extract user_id from state param (passed during /upstox/login)
    try:
        user_id = int(state) if state else None
    except (ValueError, TypeError):
        user_id = None

    if not user_id:
        return HTMLResponse(content=_oauth_error_page("Session Error","Could not identify user. Please log in to ARTHA first, then try Login with Upstox again."),status_code=400)

    conn = get_db()
    keys = {r["platform"]:dict(r) for r in conn.execute("SELECT * FROM api_keys WHERE user_id=?",(user_id,)).fetchall()}
    conn.close()
    u = keys.get("Upstox",{})
    api_key    = u.get("api_key","")
    api_secret = u.get("api_secret","")

    if api_key and api_key.startswith("eyJ"):
        return HTMLResponse(content=_oauth_error_page("API Key Field Contains a Token","Your API Key field contains a JWT token. Paste your actual Upstox Client ID instead."),status_code=400)
    if not api_key or not api_secret:
        return HTMLResponse(content=_oauth_error_page("API Key / Secret Missing","Save your Upstox API Key and Secret in API Keys first."),status_code=400)

    # Exchange code for access token
    try:
        resp = http_requests.post(
            UPSTOX_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            data={
                "code":          code,
                "client_id":     api_key,
                "client_secret": api_secret,
                "redirect_uri":  UPSTOX_REDIRECT_URI,
                "grant_type":    "authorization_code",
            },
            timeout=15
        )
        token_data = resp.json()
    except Exception as e:
        return HTMLResponse(content=_oauth_error_page("Token Exchange Failed", str(e)), status_code=500)

    if "access_token" not in token_data:
        err_msg = token_data.get("message") or token_data.get("error_description") or json.dumps(token_data)
        return HTMLResponse(content=_oauth_error_page("Token Not Received", err_msg), status_code=400)

    access_token = token_data["access_token"]
    expires_in   = token_data.get("expires_in", "86400")

    # Save access_token per user — api_key (Client ID) stays untouched
    conn = get_db()
    conn.execute(
        "UPDATE api_keys SET access_token=?, token_updated_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND platform=?",
        (access_token, user_id, "Upstox")
    )
    conn.commit(); conn.close()
    log_activity("Upstox OAuth token saved", "INFO", user_id)
    return HTMLResponse(content=_oauth_success_page(access_token, expires_in))

def _oauth_success_page(token: str, expires_in) -> str:
    short = token[:20] + "..." + token[-10:] if len(token) > 35 else token
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"/>
<title>Upstox Login Successful</title>
<style>
  *{{margin:0;padding:0;box-sizing:border-box;}}
  body{{background:#080c10;color:#e2e8f0;font-family:'Segoe UI',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;}}
  .box{{background:#0e1318;border:1px solid #1e2a34;border-radius:16px;padding:40px 48px;text-align:center;max-width:480px;border-top:4px solid #00d4aa;}}
  .icon{{font-size:52px;margin-bottom:16px;}}
  h1{{font-size:22px;font-weight:800;color:#00d4aa;margin-bottom:10px;}}
  p{{font-size:14px;color:#64748b;line-height:1.6;margin-bottom:8px;}}
  .token{{background:#141b22;border:1px solid #1e2a34;border-radius:8px;padding:10px 14px;font-family:monospace;font-size:12px;color:#a78bfa;margin:16px 0;word-break:break-all;}}
  .btn{{display:inline-block;background:#00d4aa;color:#000;font-weight:800;padding:12px 28px;border-radius:8px;text-decoration:none;font-size:14px;margin-top:8px;cursor:pointer;border:none;}}
  .note{{font-size:11px;color:#64748b;margin-top:14px;}}
  .countdown{{font-size:12px;color:#00d4aa;margin-top:8px;}}
</style>
</head><body>
<div class="box">
  <div class="icon">✅</div>
  <h1>Login Successful!</h1>
  <p>Your Upstox access token has been saved automatically.</p>
  <div class="token">{short}</div>
  <p style="color:#64748b;font-size:12px;">Valid until 3:30 AM tomorrow</p>
  <div class="countdown" id="cd">Closing in 3s...</div>
  <br/>
  <button class="btn" onclick="closeAndRefresh()">← Back to Dashboard</button>
  <div class="note">The dashboard will reload automatically with your live holdings.</div>
</div>
<script>
  function closeAndRefresh() {{
    if (window.opener) {{
      window.opener.location.reload();
      window.close();
    }} else {{
      window.location.href = '/';
    }}
  }}
  // Auto close after 3 seconds
  let t = 3;
  const cd = document.getElementById('cd');
  const iv = setInterval(() => {{
    t--;
    if (cd) cd.textContent = 'Closing in ' + t + 's...';
    if (t <= 0) {{ clearInterval(iv); closeAndRefresh(); }}
  }}, 1000);
</script>
</body></html>"""

def _oauth_error_page(title: str, message: str) -> str:
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"/>
<title>Upstox Login Error</title>
<style>
  *{{margin:0;padding:0;box-sizing:border-box;}}
  body{{background:#080c10;color:#e2e8f0;font-family:'Segoe UI',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;}}
  .box{{background:#0e1318;border:1px solid #1e2a34;border-radius:16px;padding:40px 48px;text-align:center;max-width:480px;border-top:4px solid #ef4444;}}
  .icon{{font-size:52px;margin-bottom:16px;}}
  h1{{font-size:22px;font-weight:800;color:#ef4444;margin-bottom:10px;}}
  p{{font-size:14px;color:#94a3b8;line-height:1.6;margin-bottom:8px;}}
  .detail{{background:#141b22;border:1px solid #1e2a34;border-radius:8px;padding:10px 14px;font-family:monospace;font-size:12px;color:#f87171;margin:14px 0;text-align:left;}}
  .btn{{display:inline-block;background:#1e2a34;color:#e2e8f0;font-weight:700;padding:11px 24px;border-radius:8px;text-decoration:none;font-size:13px;margin-top:10px;}}
</style>
</head><body>
<div class="box">
  <div class="icon">❌</div>
  <h1>{title}</h1>
  <div class="detail">{message}</div>
  <a href="/" class="btn">← Back to Dashboard</a>
</div>
</body></html>"""

# ── FRONTEND ────────────────────────────────────────────────────────────────────
@app.get("/login")
def login_page():
    login_path = os.path.join(BASE_DIR, "login.html")
    with open(login_path, encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/")
def frontend():
    with open(os.path.join(BASE_DIR,"index.html"),encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

if __name__=="__main__":
    uvicorn.run(app,host="0.0.0.0",port=8000)