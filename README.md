# ARTHA AI — Multi-Broker Portfolio Dashboard with AI Signals

A full-stack, multi-user web app that pulls your holdings from **Upstox**, **Groww** and **CoinDCX** into one dashboard, and layers **Gemini-powered** signals, chat and an approval-gated "auto-trade" workflow on top.

> **Status: personal / portfolio project.** Holdings are fetched live from broker APIs. Trades placed inside ARTHA are **simulated** (written to the local database) — the app does **not** place real orders on any broker. See [Limitations](#known-limitations).

---

## Features

| Area | What it does |
|---|---|
| **Multi-user auth** | Email + password registration/login, JWT sessions (7 days), per-user data isolation |
| **Upstox** | OAuth2 login flow, live holdings and positions via the official SDK |
| **Groww** | API key + secret → daily access token (checksum flow), live stocks, ETFs and intraday positions |
| **CoinDCX** | HMAC-signed exchange balances valued at live INR ticker prices, dust (< ₹1) filtered |
| **Portfolio sync** | One click pulls holdings from all connected brokers into the DB (no hardcoded seed data) |
| **AI signals** | Gemini analyses an asset and returns signal, confidence, entry/target/stop-loss, risk level |
| **AI chat** | Market/strategy assistant using current prices as context |
| **Auto-trade (approval-gated)** | AI reviews holdings and proposes BUY/SELL decisions; **nothing executes until you approve** |
| **Rate limiter** | Built-in guard for Gemini free-tier limits with a live usage counter in the top bar |
| **Activity log** | Per-user audit trail of logins, syncs, signals and decisions |

---

## Architecture

```
┌────────────────────────────┐        ┌─────────────────────────────────┐
│  Browser                   │        │  FastAPI backend (app.py)       │
│  login.html / index.html   │ ─JWT─▶ │  /api/auth/*   register, login  │
│  vanilla JS, authFetch()   │        │  /api/portfolio, /api/trades    │
└────────────────────────────┘        │  /api/auto-trade/*, /api/chat   │
                                      │  /upstox/login, /upstox/callback│
                                      └──────────┬──────────────────────┘
                                                 │
              ┌──────────────────┬───────────────┼───────────────┬───────────────┐
              ▼                  ▼               ▼               ▼               ▼
         SQLite (per-user    Upstox API      Groww API      CoinDCX API     Gemini API
         rows via user_id)   (OAuth2)        (token+SHA256) (HMAC-SHA256)   (signals/chat)
```

**Request flow:** the browser stores the JWT in `localStorage`; every API call goes through `authFetch()`, which adds the `Authorization: Bearer` header and redirects to `/login` on a 401. Backend routes depend on `get_current_user`, and every query is scoped by `user_id`.

### Data model (SQLite)

`users` · `api_keys` · `portfolio` · `trades` · `signals` · `auto_trade_log` · `auto_trade_settings` · `activity_logs` · `prices`

All tables except `prices` carry a `user_id`.

---

## Tech stack

- **Backend:** Python 3.10+, FastAPI, Uvicorn, SQLite, PyJWT
- **Frontend:** single-page vanilla HTML/CSS/JS (no build step)
- **Integrations:** `upstox-python-sdk`, Groww REST API, CoinDCX REST API, Google Gemini
- **Deployment:** AWS EC2 (t2.micro), Nginx reverse proxy, systemd

---

## Project structure

```
artha_ai/
├── app.py              # FastAPI app: auth, broker integrations, AI, DB
├── index.html          # Dashboard SPA
├── login.html          # Login / register page
├── requirements.txt
├── nginx.conf          # Reverse proxy config
├── artha.service       # systemd unit
├── DEPLOY.md           # EC2 deployment walkthrough
└── artha_trading.db    # Created on first run (git-ignored)
```

---

## Getting started (local)

```bash
git clone https://github.com/<your-username>/artha-ai.git
cd artha-ai

python -m venv venv
# Windows: venv\Scripts\activate     Linux/Mac: source venv/bin/activate

pip install -r requirements.txt
```

Set environment variables (never hardcode these):

```bash
# Linux/Mac
export GEMINI_API_KEY="your-gemini-key"
export JWT_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"

# Windows PowerShell
$env:GEMINI_API_KEY="your-gemini-key"
$env:JWT_SECRET="your-long-random-string"
```

> If `JWT_SECRET` is not set, a random one is generated on every start, which **logs all users out on each restart**. Set it explicitly.

Run:

```bash
uvicorn app:app --reload
```

Open `http://127.0.0.1:8000` → create an account → add broker keys under **API Keys**.

---

## Connecting your brokers

### Upstox (OAuth2, token expires daily)
1. Create an app at `account.upstox.com/developer/apps`.
2. Set the **Redirect URL** to exactly match `UPSTOX_REDIRECT_URI` in `app.py`
   (local: `http://127.0.0.1:8000/upstox/callback`).
3. In ARTHA → **API Keys**: save the Client ID (API Key) and Client Secret.
4. Click **Login with Upstox** → authenticate on Upstox's own page → token is saved automatically.
5. Repeat daily (token lifetime ~1 day).

### Groww (token expires daily ~6 AM IST)
1. Generate an API key + secret at `groww.in/trade-api/api-keys`.
2. Save both in **API Keys**.
3. Approve the session in the Groww app, then click **Generate Token**.

### CoinDCX (no daily login)
1. Create an API key with read access in CoinDCX → Settings → API Management.
2. Save key + secret, click **Test Connection**.

Then click **Sync Portfolio** in the top bar.

---

## API overview

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/api/auth/register` · `/login` · `/me` · `/change-password` | POST/GET | – / JWT | Account management |
| `/api/keys` · `/api/keys/token-status` | GET/POST | JWT | Broker key storage and token status |
| `/api/portfolio/sync` | POST | JWT | Pull holdings from all brokers |
| `/api/portfolio` | GET | JWT | Aggregated portfolio with P&L |
| `/api/upstox/portfolio` · `/api/groww/portfolio` · `/api/coindcx/portfolio` | GET | JWT | Live per-broker data |
| `/api/groww/generate-token` · `/api/coindcx/test-connection` | POST | JWT | Broker auth helpers |
| `/api/analyze` · `/api/signals` | POST/GET | JWT | AI signal generation and history |
| `/api/chat` | POST | JWT | AI assistant |
| `/api/auto-trade/analyze` · `/pending` · `/history` · `/approve` · `/reject/{id}` · `/settings` | mixed | JWT | Approval-gated AI decisions |
| `/api/trade` · `/api/trades` | POST/GET | JWT | Simulated trades |
| `/api/logs` | GET/DELETE | JWT | Activity log |
| `/upstox/login` · `/upstox/callback` | GET | JWT via `?token=` / state | OAuth flow |

Interactive docs: `http://127.0.0.1:8000/docs`.

---

## Deployment (AWS EC2 free tier)

Full walkthrough in [`DEPLOY.md`](DEPLOY.md). Summary:

1. Launch Ubuntu 22.04 `t2.micro`; open ports 22 (your IP only), 80, 443.
2. Copy files, create a venv, `pip install -r requirements.txt`.
3. Install `artha.service` (set `GEMINI_API_KEY`, `JWT_SECRET`) → `systemctl enable --now artha`.
4. Install `nginx.conf` as a site → reverse proxy `:80` → `127.0.0.1:8000`.
5. Update the Upstox redirect URI (in `app.py` **and** the Upstox portal) to your public URL.

For anything beyond personal use, put it behind a domain with HTTPS (Let's Encrypt) — see Security.

---

## Known limitations

Read these before relying on the app.

- **Trades are simulated.** "Approve & Execute" updates the local DB only; no broker order is sent.
- **Prices are partly simulated.** A background thread simulates price movement for the price ticker. Broker holdings use broker-reported prices at sync time; they are not a streaming feed.
- **No cost basis for CoinDCX exchange holdings** (the API doesn't return it), so P&L there is not meaningful. CoinDCX **Web3 wallet** has no public API and is not fetched.
- **Groww Mutual Funds are not available** — the Groww Trading API exposes only DEMAT holdings (stocks/ETFs) and positions.
- **AI output is not financial advice.** Signals come from an LLM given limited context (price and change %). Treat them as a brainstorming aid; they are not backtested.
- **Daily re-authentication** is required for Upstox and Groww.
- **Gemini free-tier quotas are tight** and model availability changes; the code currently uses `google.generativeai`, which is deprecated in favour of `google-genai`.

---

## Security notes

This project handles brokerage credentials. Current state, honestly:

| Item | Current | Should be |
|---|---|---|
| Password hashing | SHA-256 with a static salt | `bcrypt` / `argon2` with per-user salt |
| Broker keys/tokens in DB | Plaintext in SQLite | Encrypted at rest (e.g. Fernet with a key from env/KMS) |
| Transport | HTTP on EC2 IP | HTTPS (Let's Encrypt) — JWTs and API keys currently cross the network unencrypted |
| JWT storage | `localStorage` | `HttpOnly` cookie (mitigates XSS token theft) |
| Upstox login | JWT passed as `?token=` query param (needed for popup) | Short-lived one-time state token |
| Rate limiting on auth | None | Throttle `/api/auth/login` |

**Do not expose a deployment with real broker keys to the public internet until at least HTTPS and password hashing are fixed.**

Never commit: `*.pem`, `*.db`, `.env`, API keys. See `.gitignore`.

---

## Roadmap

- [ ] bcrypt/argon2 password hashing; encrypt stored broker secrets
- [ ] HTTPS + domain; HttpOnly cookie sessions
- [ ] Migrate to `google-genai`
- [ ] Real order placement behind an explicit confirm step (Upstox/Groww order APIs)
- [ ] PostgreSQL (RDS) instead of SQLite for multi-user concurrency
- [ ] Dockerfile + CI (lint, tests)
- [ ] Unit tests for auth, sync and rate limiter
- [ ] Historical portfolio snapshots and charts

---

## Disclaimer

For educational and personal use only. Not investment advice. You are responsible for any decisions made using this tool and for complying with each broker's API terms.

## License

MIT (add a `LICENSE` file before publishing).
