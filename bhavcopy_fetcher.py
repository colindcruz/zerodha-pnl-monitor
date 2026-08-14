"""
NSE F&O Bhavcopy fetcher
=========================
Downloads and caches NSE's daily F&O bhavcopy (UDiFF format, in use since
Jul 2024) so historical option prices remain queryable even after a
contract's expiry drops it out of Zerodha's live instrument list.

Source: https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_<YYYYMMDD>_F_0000.csv.zip
Returns 404 for weekends/holidays.

CLI usage:
  python bhavcopy_fetcher.py --date 2026-08-10 --symbol NIFTY
      # lists all NIFTY strikes/expiries traded that day
  python bhavcopy_fetcher.py --date 2026-08-10 --symbol NIFTY --strike 24800 --type CE --expiry 2026-08-11
      # single contract's OHLC + settlement + underlying price
"""

import argparse
import csv
import io
import zipfile
from datetime import date, datetime
from pathlib import Path

import requests

CACHE_DIR = Path(__file__).parent / "bhavcopy_cache"
BASE_URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch_bhavcopy(trade_date: date) -> Path | None:
    """Downloads (or reuses cached) bhavcopy CSV for trade_date. Returns None on weekend/holiday (404)."""
    ymd = trade_date.strftime("%Y%m%d")
    csv_path = CACHE_DIR / f"BhavCopy_NSE_FO_{ymd}.csv"
    if csv_path.exists():
        return csv_path

    resp = requests.get(BASE_URL.format(ymd=ymd), headers=HEADERS, timeout=30)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()

    CACHE_DIR.mkdir(exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        inner_name = zf.namelist()[0]
        csv_path.write_bytes(zf.read(inner_name))
    return csv_path


def _parse_row(row: dict) -> dict:
    """Casts the columns we care about; falls back to settlement price when OHLC is 0 (no trades that day)."""
    opn, hgh, lw, cls = (float(row[k]) for k in ("OpnPric", "HghPric", "LwPric", "ClsPric"))
    settle = float(row["SttlmPric"])
    underlying = float(row["UndrlygPric"])
    no_trades = opn == 0 and hgh == 0 and lw == 0 and cls == 0
    # On expiry day NSE reprints SttlmPric as the underlying's close for that row instead of
    # the option's own settlement value — don't use it as an OHLC substitute in that case.
    settle_is_usable = settle != underlying
    use_settle = no_trades and settle_is_usable
    return {
        "symbol": row["TckrSymb"],
        "expiry": row["XpryDt"],
        "strike": float(row["StrkPric"]),
        "option_type": row["OptnTp"],
        "open": settle if use_settle else opn,
        "high": settle if use_settle else hgh,
        "low": settle if use_settle else lw,
        "close": settle if use_settle else cls,
        "settle_price": settle,
        "underlying_price": underlying,
        "oi": int(row["OpnIntrst"]),
        "volume": int(row["TtlTradgVol"]),
        "no_trades": no_trades,
    }


def get_chain(trade_date: date, symbol: str = "NIFTY", expiry: str | None = None) -> list[dict]:
    """All option rows for symbol on trade_date (index options only), optionally filtered to one expiry (YYYY-MM-DD)."""
    csv_path = fetch_bhavcopy(trade_date)
    if csv_path is None:
        return []

    rows = []
    with csv_path.open(newline="") as f:
        for row in csv.DictReader(f):
            if row["FinInstrmTp"] != "IDO" or row["TckrSymb"] != symbol:
                continue
            if expiry and row["XpryDt"] != expiry:
                continue
            rows.append(_parse_row(row))
    return rows


def get_contract(
    trade_date: date, symbol: str, strike: float, option_type: str, expiry: str
) -> dict | None:
    """Single contract's EOD data. option_type is 'CE' or 'PE'. expiry is 'YYYY-MM-DD'."""
    for row in get_chain(trade_date, symbol, expiry):
        if row["strike"] == strike and row["option_type"] == option_type:
            return row
    return None


def get_underlying_price(trade_date: date, symbol: str = "NIFTY") -> float | None:
    chain = get_chain(trade_date, symbol)
    return chain[0]["underlying_price"] if chain else None


def _main():
    ap = argparse.ArgumentParser(description="Fetch NSE F&O bhavcopy option data")
    ap.add_argument("--date", required=True, help="Trade date, YYYY-MM-DD")
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--strike", type=float)
    ap.add_argument("--type", choices=["CE", "PE"])
    ap.add_argument("--expiry", help="YYYY-MM-DD; required with --strike/--type")
    args = ap.parse_args()

    trade_date = datetime.strptime(args.date, "%Y-%m-%d").date()

    if args.strike is not None and args.type:
        if not args.expiry:
            ap.error("--expiry is required when --strike/--type are given")
        row = get_contract(trade_date, args.symbol, args.strike, args.type, args.expiry)
        if row is None:
            print(f"No contract found for {args.symbol} {args.strike}{args.type} exp {args.expiry} on {args.date}")
            print("(holiday/weekend, wrong expiry, or strike didn't exist that day)")
            return
        print(f"{args.symbol} {row['strike']:.0f}{row['option_type']} exp {row['expiry']}  [{args.date}]")
        print(f"  underlying (spot): {row['underlying_price']:.2f}")
        print(f"  O:{row['open']:.2f}  H:{row['high']:.2f}  L:{row['low']:.2f}  C:{row['close']:.2f}")
        # On expiry day NSE reprints SttlmPric as the underlying's close for that row,
        # not the option's own settlement value — skip it there rather than show a bogus number.
        if row["settle_price"] != row["underlying_price"]:
            print(f"  settle: {row['settle_price']:.2f}")
        print(f"  OI:{row['oi']:,}  volume:{row['volume']:,}" + ("  (no trades — showing settlement price)" if row["no_trades"] else ""))
        return

    chain = get_chain(trade_date, args.symbol, args.expiry)
    if not chain:
        print(f"No data for {args.symbol} on {args.date} (holiday/weekend, or no such expiry).")
        return
    spot = chain[0]["underlying_price"]
    expiries = sorted({r["expiry"] for r in chain})
    print(f"{args.symbol}  [{args.date}]  spot: {spot:.2f}")
    print(f"Expiries available: {', '.join(expiries)}")
    print(f"{len(chain)} contracts total. Use --expiry to narrow, --strike/--type/--expiry for a single contract.")


if __name__ == "__main__":
    _main()
