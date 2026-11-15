# BLUE WHALE PRO – NSE

Streamlit starter scanner for the Nifty 500 universe using the Upstox Analytics Token (read-only).

## Streamlit Secrets

Add these in Streamlit Community Cloud → App → Settings → Secrets:

```toml
APP_PASSWORD = "choose-a-private-password"
UPSTOX_ACCESS_TOKEN = "your-upstox-analytics-token"
```

Never commit tokens or passwords to GitHub.

## Features
- Loads Nifty 500 constituents from Nifty Indices.
- Maps symbols to Upstox NSE equity instrument keys.
- Requests a snapshot for up to 500 instruments using Upstox Full Market Quotes V3.
- Runs the original-style Blue Whale POC rule on a selected rotating batch.
- Shows latest alerts during the current app session.

## Important limitations
- This is a starter build and must be validated against the original Pine Script before live trading decisions.
- Historical candle scans are API-intensive. The app scans a selected batch (10–50 stocks) per run rather than making thousands of calls for all 500 at once.
- Alert history is session-only in this starter build. A database (for example Supabase) is required for durable history across app restarts.
- No orders are placed. The Upstox Analytics Token is read-only.
