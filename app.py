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
NIFTY500_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
UPSTOX_NSE_INSTRUMENTS = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"

@st.cache_data(ttl=3600, show_spinner=False)
def get_nifty500():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36",
        "Accept": "text/csv,text/plain,*/*",
        "Referer": "https://www.nseindia.com/",
        "Origin": "https://www.nseindia.com",
    }
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
    data = upstox_get("/v3/market-quote/quotes", {"instrument_key": ",".join(keys_tuple)})
    rows = []
    for key, q in data.items():
        # Upstox response may include a symbol field; preserve it as a fallback.
        rows.append({
            "Instrument": str(key),
            "Quote Symbol": q.get("symbol") or q.get("trading_symbol") or q.get("short_name"),
            "Instrument Token": q.get("instrument_token") or q.get("instrument_key"),
            "LTP": q.get("last_price"),
            "Change": q.get("net_change"),
            "Volume": q.get("volume"),
            "Last trade time": q.get("last_trade_time"),
        })
    return pd.DataFrame(rows)


def parse_candle_rows(rows):
    if not rows:
        return pd.DataFrame(columns=["ts","open","high","low","close","volume","oi"])
    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume","oi"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Asia/Kolkata")
    return df.sort_values("ts").drop_duplicates(subset=["ts"], keep="last").reset_index(drop=True)

def candles(instrument_key, unit, interval, days_back):
    # Upstox Historical Candle V3 documented retrieval limits:
    # minutes 1-15: 1 month; minutes >15 and all hours: 1 quarter;
    # days: 1 decade; weeks/months: no stated limit.
    interval_num = int(interval)
    if unit == "minutes":
        max_days = 28 if interval_num <= 15 else 80
    elif unit == "hours":
        max_days = 80
    elif unit == "days":
        max_days = 3650
    else:  # weeks/months
        max_days = 3650  # conservative request window; API itself has no stated cap
    days_back = min(int(days_back), max_days)

    to_date = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    from_date = to_date - timedelta(days=days_back)
    path = f"/v3/historical-candle/{requests.utils.quote(instrument_key, safe='')}/{unit}/{interval}/{to_date.isoformat()}/{from_date.isoformat()}"
    d = upstox_get(path)
    return parse_candle_rows(d.get("candles", []))

def intraday_candles(instrument_key, unit, interval):
    # Upstox Intraday Candle V3 supplies current trading-day candles only.
    path = f"/v3/historical-candle/intraday/{requests.utils.quote(instrument_key, safe='')}/{unit}/{interval}"
    d = upstox_get(path)
    return parse_candle_rows(d.get("candles", []))

def merge_candle_sets(historical, intraday):
    if historical.empty:
        return intraday.copy()
    if intraday.empty:
        return historical.copy()
    merged = pd.concat([historical, intraday], ignore_index=True)
    return merged.sort_values("ts").drop_duplicates(subset=["ts"], keep="last").reset_index(drop=True)

def is_nse_regular_session(now=None):
    now = now or datetime.now(ZoneInfo("Asia/Kolkata"))
    return now.weekday() < 5 and (now.hour, now.minute) >= (9, 15) and (now.hour, now.minute) < (15, 30)


def map_poc_to_base(htf, base):
    """
    Pine formula:
    (previous HTF high + previous HTF low + previous HTF close + current HTF open) / 4
    Each HTF POC is aligned to base candles using the active HTF candle timestamp.
    """
    if len(htf) < 2 or base.empty:
        return pd.Series(index=base.index, dtype="float64")
    h = htf[["ts", "open", "high", "low", "close"]].copy().sort_values("ts")
    h["poc"] = (h["high"].shift(1) + h["low"].shift(1) + h["close"].shift(1) + h["open"]) / 4.0
    h = h[["ts", "poc"]].dropna().sort_values("ts")
    b = base[["ts"]].copy()
    aligned = pd.merge_asof(b.sort_values("ts"), h, on="ts", direction="backward")
    return pd.Series(aligned["poc"].to_numpy(), index=base.index, dtype="float64")

def get_closed_base_candles(df, tf):
    if df.empty:
        return df
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    # During live NSE hours, exclude a candle that has not yet finished.
    if now.weekday() < 5 and (now.hour, now.minute) >= (9, 15) and (now.hour, now.minute) < (15, 30):
        minutes = {"30m": 30, "1H": 60, "2H": 120, "3H": 180, "4H": 240}[tf]
        cutoff = pd.Timestamp(now)
        return df[df["ts"] + pd.to_timedelta(minutes, unit="m") <= cutoff].copy()
    # Outside trading hours, the latest available candle is treated as closed.
    return df.copy()

def make_condition(close, poc, direction, win_bars=3):
    crosses = []
    for i in range(len(close)):
        if i == 0 or pd.isna(poc.iloc[i]) or pd.isna(poc.iloc[i-1]):
            crosses.append(False)
            continue
        if direction == "BUY":
            crosses.append(close.iloc[i] > poc.iloc[i] and close.iloc[i-1] <= poc.iloc[i-1])
        else:
            crosses.append(close.iloc[i] < poc.iloc[i] and close.iloc[i-1] >= poc.iloc[i-1])
    flags = []
    last_cross = None
    for i, crossed in enumerate(crosses):
        if crossed:
            last_cross = i
        within = last_cross is not None and (i - last_cross) <= win_bars
        if direction == "BUY":
            flags.append(within and close.iloc[i] > poc.iloc[i])
        else:
            flags.append(within and close.iloc[i] < poc.iloc[i])
    return pd.Series(flags, index=close.index, dtype=bool)


def scan_one(symbol, key, tf):
    # Selected timeframe history: 80 days is safely inside Upstox V3's one-quarter
    # limit for 30m and 1h-4h candles.
    unit, interval, base_days = {
        "30m": ("minutes","30",80),
        "1H": ("hours","1",80),
        "2H": ("hours","2",80),
        "3H": ("hours","3",80),
        "4H": ("hours","4",80),
    }[tf]

    base_raw = candles(key, unit, interval, base_days)
    h4 = candles(key, "hours", "4", 80)
    daily = candles(key, "days", "1", 400)     # within 10-year V3 daily limit
    weekly = candles(key, "weeks", "1", 900)  # weekly V3 has no stated retrieval cap

    # During NSE regular hours, append today's live intraday candles to historical
    # candles. Outside market hours, use the latest completed historical session.
    if is_nse_regular_session():
        try:
            base_raw = merge_candle_sets(base_raw, intraday_candles(key, unit, interval))
        except Exception:
            pass
        try:
            h4 = merge_candle_sets(h4, intraday_candles(key, "hours", "4"))
        except Exception:
            pass
        try:
            daily = merge_candle_sets(daily, intraday_candles(key, "days", "1"))
        except Exception:
            pass

    base = get_closed_base_candles(base_raw, tf)
    if min(len(base), len(h4), len(daily), len(weekly)) < 4:
        return None

    p4 = map_poc_to_base(h4, base)
    pd_ = map_poc_to_base(daily, base)
    pw = map_poc_to_base(weekly, base)
    valid = p4.notna() & pd_.notna() & pw.notna()
    base = base.loc[valid].copy()
    p4, pd_, pw = p4.loc[valid], pd_.loc[valid], pw.loc[valid]
    if len(base) < 4:
        return None

    # IMPORTANT: POC formulas and Blue Whale entry conditions above are unchanged.
    # Only the scan window changes: search for fresh entry events in the last 30 days.
    close = base["close"].astype(float)
    up4 = make_condition(close, p4, "BUY", 3)
    upd = make_condition(close, pd_, "BUY", 3)
    upw = make_condition(close, pw, "BUY", 3)
    dn4 = make_condition(close, p4, "SELL", 3)
    dnd = make_condition(close, pd_, "SELL", 3)
    dnw = make_condition(close, pw, "SELL", 3)

    up_cond = up4 & upd & upw & (close > p4) & (close > pd_) & (close > pw)
    dn_cond = dn4 & dnd & dnw & (close < p4) & (close < pd_) & (close < pw)

    # Same Pine-style new-signal guard: current condition true, previous false.
    buy_events = up_cond & ~up_cond.shift(1, fill_value=False)
    sell_events = dn_cond & ~dn_cond.shift(1, fill_value=False)

    cutoff = pd.Timestamp(datetime.now(ZoneInfo("Asia/Kolkata")) - timedelta(days=30))
    recent_mask = base["ts"] >= cutoff
    event_mask = (buy_events | sell_events) & recent_mask
    event_positions = [i for i, flag in enumerate(event_mask.tolist()) if flag]
    if not event_positions:
        return None

    # Show the most recent Blue Whale event found in the 30-day lookback.
    i = event_positions[-1]
    side = "BUY" if bool(buy_events.iloc[i]) else "SELL"
    return {
        "Stock": symbol, "Signal": side, "Timeframe": tf,
        "Entry": round(float(close.iloc[i]), 2),
        "4H POC": round(float(p4.iloc[i]), 2),
        "Daily POC": round(float(pd_.iloc[i]), 2),
        "Weekly POC": round(float(pw.iloc[i]), 2),
        "Candle time": str(base.ts.iloc[i])
    }

st.title("🐋 BLUE WHALE PRO – NSE")
st.caption("Nifty 500 | Past 30 days Blue Whale entry alerts | Original POC entry logic unchanged | No Stop Loss or exit rules")

now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
if is_nse_regular_session(now_ist):
    st.success("NSE regular session is open. Scanner uses today's intraday candles where available; run a scan to refresh.")
else:
    st.info(f"NSE regular session is closed. Showing the latest available completed market candles. Current IST: {now_ist.strftime('%d-%b-%Y %I:%M %p')}")


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
                key_to_symbol = {str(k).strip(): s for s,k in universe}
                def resolve_stock(row):
                    candidates = [row.get("Instrument"), row.get("Instrument Token")]
                    for candidate in candidates:
                        if candidate is not None and str(candidate).strip() in key_to_symbol:
                            return key_to_symbol[str(candidate).strip()]
                    quote_symbol = row.get("Quote Symbol")
                    if quote_symbol and str(quote_symbol).strip().lower() != "none":
                        return str(quote_symbol).strip()
                    # Last-resort visible identifier; never display Python None.
                    instrument = str(row.get("Instrument", "")).strip()
                    return instrument.split("|")[-1] if instrument else "UNKNOWN"
                qdf["Stock"] = qdf.apply(resolve_stock, axis=1)
                st.dataframe(qdf[["Stock","LTP","Change","Volume","Last trade time"]], use_container_width=True, hide_index=True)
            else:
                st.warning("Quote data empty. Market closed ho sakta hai ya API response empty hai.")
        except Exception as e:
            st.error(f"Quote API error: {e}")
    else:
        st.info("Live quote snapshot dekhne ke liye Refresh all 500 quotes dabao.")

with tab2:
    st.subheader("Blue Whale Scanner – Past 30 Days")
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
            st.success(f"{len(found)} stock(s) mein past 30 days ka latest Blue Whale alert mila.")
            st.dataframe(pd.DataFrame(found), use_container_width=True, hide_index=True)
        else:
            st.info("Is batch ke stocks mein past 30 days ke andar koi Blue Whale BUY/SELL entry nahi mili.")
        if errors:
            with st.expander(f"{len(errors)} API/data issue(s)"):
                st.write("\n".join(errors[:20]))

    st.divider()
    st.subheader("Kalyan Jewellers – Quick Blue Whale Check")
    st.caption("NSE symbol: KALYANKJIL | Checks whether a Blue Whale entry occurred in the past 30 days")
    kalyan_key = instrument_map.get("KALYANKJIL")
    if not kalyan_key:
        st.warning("KALYANKJIL instrument key Upstox instrument master mein nahi mila.")
    else:
        k_tf = st.selectbox("Kalyan scan timeframe", ["30m","1H","2H","3H","4H"], index=1, key="kalyan_tf")
        if st.button("Check Kalyan Jewellers", type="secondary"):
            with st.spinner("KALYANKJIL ke candles aur POC levels check ho rahe hain..."):
                try:
                    k_signal = scan_one("KALYANKJIL", kalyan_key, k_tf)
                    if k_signal:
                        st.success("Blue Whale alert mila!")
                        st.dataframe(pd.DataFrame([k_signal]), use_container_width=True, hide_index=True)
                        st.session_state.setdefault("alerts", [])
                        st.session_state.alerts.insert(0, k_signal)
                    else:
                        st.info("Selected timeframe mein past 30 days ke andar koi Blue Whale BUY/SELL entry nahi mili.")
                except Exception as e:
                    st.error(f"Kalyan scan error: {e}")

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
