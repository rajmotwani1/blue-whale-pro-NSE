import io, json, time, gzip, requests
import pandas as pd
import streamlit as st
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

st.set_page_config(page_title="BLUE WHALE PRO | NSE", page_icon="🐋", layout="wide")

# ---------- Login ----------
def secret(name, default=""):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default

app_password = secret("APP_PASSWORD", "")
if app_password:
    if not st.session_state.get("authenticated", False):
        st.title("🐋 BLUE WHALE PRO – NSE")
        pwd = st.text_input("Enter password", type="password")
        if st.button("Login", type="primary"):
            if pwd == app_password:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Incorrect password")
        st.stop()
else:
    st.warning("APP_PASSWORD secret is not configured. Add it in Streamlit Settings → Secrets.")

token = secret("UPSTOX_ACCESS_TOKEN", "")
if not token:
    st.title("🐋 BLUE WHALE PRO – NSE")
    st.error("Upstox Analytics Token is missing. Add UPSTOX_ACCESS_TOKEN in Streamlit Settings → Secrets.")
    st.code('APP_PASSWORD = "your-private-password"\nUPSTOX_ACCESS_TOKEN = "paste-your-token-here"')
    st.stop()

BASE = "https://api.upstox.com"
HEADERS = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
NIFTY500_URL = "https://www.niftyindices.com/Index_Constituent/ind_nifty500list.csv"
UPSTOX_NSE_INSTRUMENTS = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"

@st.cache_data(ttl=3600, show_spinner=False)
def get_nifty500():
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.niftyindices.com/"}
    r = requests.get(NIFTY500_URL, headers=headers, timeout=25)
    r.raise_for_status()
    df = pd.read_csv(io.BytesIO(r.content))
    df.columns = [str(c).strip() for c in df.columns]
    sym_col = next((c for c in df.columns if c.lower() in ("symbol", "symbols")), None)
    if not sym_col:
        raise ValueError("Nifty 500 CSV mein Symbol column nahi mila.")
    return sorted(df[sym_col].dropna().astype(str).str.strip().unique().tolist())

@st.cache_data(ttl=3600, show_spinner=False)
def get_instrument_map():
    r = requests.get(UPSTOX_NSE_INSTRUMENTS, timeout=35)
    r.raise_for_status()
    raw = gzip.decompress(r.content)
    items = json.loads(raw.decode("utf-8"))
    out = {}
    for x in items:
        if x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ":
            out[str(x.get("trading_symbol", "")).upper()] = x.get("instrument_key")
    return out

def upstox_get(path, params=None):
    r = requests.get(BASE + path, headers=HEADERS, params=params, timeout=20)
    if r.status_code == 401:
        raise RuntimeError("Upstox token invalid/expired. Analytics Token ko dobara check karo.")
    r.raise_for_status()
    payload = r.json()
    if payload.get("status") not in (None, "success"):
        raise RuntimeError(str(payload))
    return payload.get("data", {})

@st.cache_data(ttl=20, show_spinner=False)
def get_quotes(keys_tuple, token_fingerprint):
    # Token itself is not used as a cache key in the UI.
    data = upstox_get("/v3/market-quote/quotes", {"instrument_key": ",".join(keys_tuple)})
    rows = []
    for key, q in data.items():
        rows.append({
            "Instrument": key,
            "LTP": q.get("last_price"),
            "Change": q.get("net_change"),
            "Volume": q.get("volume"),
            "Last trade time": q.get("last_trade_time"),
        })
    return pd.DataFrame(rows)

def candles(instrument_key, unit, interval, days_back):
    to_date = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    from_date = to_date - timedelta(days=days_back)
    path = f"/v3/historical-candle/{requests.utils.quote(instrument_key, safe='')}/{unit}/{interval}/{to_date.isoformat()}/{from_date.isoformat()}"
    d = upstox_get(path)
    rows = d.get("candles", [])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume","oi"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Asia/Kolkata")
    return df.sort_values("ts").reset_index(drop=True)

def poc_for_latest(htf):
    if len(htf) < 2:
        return None
    prev = htf.iloc[-2]
    cur = htf.iloc[-1]
    return (float(prev.high) + float(prev.low) + float(prev.close) + float(cur.open)) / 4.0

def crosses_up(series, level, bars=3):
    vals = series.tail(bars + 1).astype(float).tolist()
    return any(vals[i-1] <= level and vals[i] > level for i in range(1, len(vals)))

def crosses_down(series, level, bars=3):
    vals = series.tail(bars + 1).astype(float).tolist()
    return any(vals[i-1] >= level and vals[i] < level for i in range(1, len(vals)))

def scan_one(symbol, key, tf):
    # Scan a rotating batch to respect API limits. Each stock needs multiple historical-data calls.
    unit, interval, base_days = {
        "30m": ("minutes","30",45),
        "1H": ("hours","1",75),
        "2H": ("hours","2",100),
        "3H": ("hours","3",120),
        "4H": ("hours","4",150),
    }[tf]
    base = candles(key, unit, interval, base_days)
    h4 = candles(key, "hours", "4", 180)
    daily = candles(key, "days", "1", 400)
    weekly = candles(key, "weeks", "1", 900)
    if min(len(base), len(h4), len(daily), len(weekly)) < 4:
        return None
    # Use the latest completed base candle; historical POC levels use the latest HTF completed/current pair.
    base = base.iloc[:-1].copy()
    if len(base) < 4:
        return None
    p4, pd_, pw = poc_for_latest(h4), poc_for_latest(daily), poc_for_latest(weekly)
    if None in (p4, pd_, pw):
        return None
    close = base["close"].astype(float)
    buy = all(crosses_up(close, p, 3) for p in (p4, pd_, pw)) and close.iloc[-1] > max(p4,pd_,pw)
    sell = all(crosses_down(close, p, 3) for p in (p4,pd_,pw)) and close.iloc[-1] < min(p4,pd_,pw)
    if not (buy or sell):
        return None
    recent = base.tail(3)
    side = "BUY" if buy else "SELL"
    sl = float(recent.low.min()) if buy else float(recent.high.max())
    return {
        "Stock": symbol, "Signal": side, "Timeframe": tf,
        "Entry": round(float(close.iloc[-1]), 2), "Stop Loss": round(sl, 2),
        "4H POC": round(p4, 2), "Daily POC": round(pd_, 2), "Weekly POC": round(pw, 2),
        "Candle time": str(base.ts.iloc[-1])
    }

st.title("🐋 BLUE WHALE PRO – NSE")
st.caption("Nifty 500 | Upstox Analytics Token | Read-only market scanner")

try:
    symbols = get_nifty500()
    instrument_map = get_instrument_map()
    universe = [(s, instrument_map[s]) for s in symbols if s in instrument_map]
except Exception as e:
    st.error(f"Stock list load nahi hui: {e}")
    st.info("Nifty Indices ya Upstox instrument file temporarily unavailable ho sakti hai. Thodi der baad Refresh try karo.")
    st.stop()

st.success(f"Nifty 500 watchlist loaded: {len(symbols)} constituents | Upstox NSE instruments matched: {len(universe)}")
c1,c2,c3 = st.columns(3)
c1.metric("Nifty 500 constituents", len(symbols))
c2.metric("Upstox matched stocks", len(universe))
c3.metric("Data mode", "Read-only")

tab1, tab2, tab3 = st.tabs(["Market Watch", "Blue Whale Scanner", "Last Alerts"])

with tab1:
    st.subheader("Nifty 500 – Live Quote Snapshot")
    st.caption("Upstox Full Market Quotes V3 supports up to 500 instrument keys per request. Quotes refresh when you press the button.")
    if st.button("Refresh all 500 quotes", type="primary"):
        keys = tuple(k for _,k in universe[:500])
        try:
            qdf = get_quotes(keys, token[-6:])
            if not qdf.empty:
                key_to_symbol = {k:s for s,k in universe}
                qdf["Stock"] = qdf["Instrument"].map(key_to_symbol)
                st.dataframe(qdf[["Stock","LTP","Change","Volume","Last trade time"]], use_container_width=True, hide_index=True)
            else:
                st.warning("Quote data empty. Market closed ho sakta hai ya API response empty hai.")
        except Exception as e:
            st.error(f"Quote API error: {e}")
    else:
        st.info("Live quote snapshot dekhne ke liye Refresh all 500 quotes dabao.")

with tab2:
    st.subheader("Blue Whale Signal Scanner")
    st.warning("500 stocks watchlist mein hain. Historical candle API limits ko dhyan mein rakhte hue har scan mein selected batch scan hota hai; isse poore 500 ko ek saath scan karne ka claim nahi kiya ja raha.")
    tf = st.selectbox("Timeframe", ["30m","1H","2H","3H","4H"])
    batch_size = st.selectbox("Stocks per scan batch", [10,20,25,50], index=2)
    total_batches = max(1, (len(universe) + batch_size - 1)//batch_size)
    batch_no = st.number_input("Batch number", min_value=1, max_value=total_batches, value=1, step=1)
    start = (int(batch_no)-1)*batch_size
    batch = universe[start:start+batch_size]
    st.caption(f"Batch {batch_no}/{total_batches}: {len(batch)} stocks")
    if st.button("Run Blue Whale scan", type="primary"):
        found, errors = [], []
        progress = st.progress(0)
        status = st.empty()
        for i,(sym,key) in enumerate(batch):
            status.write(f"Scanning {sym} ({i+1}/{len(batch)})...")
            try:
                signal = scan_one(sym,key,tf)
                if signal:
                    found.append(signal)
                    st.session_state.setdefault("alerts", [])
                    st.session_state.alerts.insert(0, signal)
                time.sleep(0.15)
            except Exception as e:
                errors.append(f"{sym}: {str(e)[:180]}")
            progress.progress((i+1)/len(batch))
        status.write("Scan complete.")
        st.session_state["last_scan"] = found
        st.session_state["scan_errors"] = errors
        if found:
            st.success(f"{len(found)} Blue Whale alert(s) found in this batch.")
            st.dataframe(pd.DataFrame(found), use_container_width=True, hide_index=True)
        else:
            st.info("Is batch mein abhi koi confirmed Blue Whale signal nahi mila.")
        if errors:
            with st.expander(f"{len(errors)} API/data issue(s)"):
                st.write("\n".join(errors[:20]))

with tab3:
    st.subheader("Last Blue Whale Alerts")
    alerts = st.session_state.get("alerts", [])
    if alerts:
        # Keep only most recent 200 in current app session.
        st.dataframe(pd.DataFrame(alerts[:200]), use_container_width=True, hide_index=True)
        if st.button("Clear alert list"):
            st.session_state["alerts"] = []
            st.rerun()
        st.caption("Important: Is starter version mein alert history current app session tak hai. Permanent history across restarts ke liye next phase mein Supabase/database connect karna hoga.")
    else:
        st.info("Abhi alerts nahi hain. Scanner tab mein Run Blue Whale scan dabao.")

st.divider()
st.caption("Educational scanner only. Signals are rule-based indicators, not investment advice. No orders are placed. Verify signals independently before trading.")
