"""SQLite cache over pytefas. Also the seed/update CLI: python -m fonlu.store --days 90"""

import json
import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from pytefas import Crawler

from . import kap

# Docker'da veritabani koda degil bir volume'e yaziliyor; FONLU_DB ile yolu degistir.
DB_PATH = Path(os.environ.get("FONLU_DB")
               or Path(__file__).resolve().parent.parent / "fonlu.db")
KINDS = ("YAT", "EMK", "BYF")

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    fund_code TEXT NOT NULL,
    date TEXT NOT NULL,
    kind TEXT,
    fund_name TEXT,
    price REAL,
    shares_outstanding REAL,
    investor_count INTEGER,
    portfolio_size REAL,
    PRIMARY KEY (fund_code, date)
);
CREATE INDEX IF NOT EXISTS prices_date ON prices(date);

-- Only the most recent snapshot per fund is kept; allocation is a JSON map of
-- non-zero pct columns, since normalizing 54 percentage columns buys nothing.
CREATE TABLE IF NOT EXISTS breakdown (
    fund_code TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    allocation TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kap_disclosures (
    disclosure_index INTEGER PRIMARY KEY,
    fund_code TEXT NOT NULL,
    publish_date TEXT NOT NULL,
    title TEXT,
    subject TEXT,
    summary TEXT,
    disclosure_class TEXT,
    attachment_count INTEGER
);
CREATE INDEX IF NOT EXISTS kap_fund ON kap_disclosures(fund_code, publish_date DESC);

-- KAP portfoy dagilim raporundan cikarilan kalemler. Talep uzerine doluyor (PDF
-- indirip ayristirmak yavas), bir daha ayni PDF'e gidilmiyor. Rapor tarihi PK'da:
-- ay bazli kiyas icin fon basina birden fazla rapor tutuluyor.
CREATE TABLE IF NOT EXISTS holdings (
    fund_code TEXT NOT NULL,
    report_date TEXT NOT NULL,
    section TEXT NOT NULL,
    code TEXT NOT NULL,
    isin TEXT,
    issuer TEXT,
    value REAL,
    weight_pct REAL,
    disclosure_index INTEGER,
    PRIMARY KEY (fund_code, report_date, section, code)
);

CREATE TABLE IF NOT EXISTS watchlist (
    fund_code TEXT PRIMARY KEY,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code TEXT NOT NULL,
    units REAL NOT NULL CHECK (units > 0),
    buy_date TEXT NOT NULL,
    buy_price REAL NOT NULL CHECK (buy_price > 0),
    note TEXT
);
"""


def connect(path=None):
    # check_same_thread=False: FastAPI runs a sync dependency and its endpoint on
    # different threadpool threads. Each request still gets its own connection, so
    # nothing is shared concurrently. WAL keeps reads working while a refresh writes.
    conn = sqlite3.connect(path or DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _migrate(conn)
    conn.executescript(SCHEMA)
    return conn


def _migrate(conn):
    """Eski holdings semasini dusur.

    holdings tek fon icin tek rapor tutuyordu (fund_code, ticker); simdi rapor
    tarihi ve varlik bolumu de anahtarda. CREATE TABLE IF NOT EXISTS eski tabloyu
    donusturmedigi icin acikca dusuruluyor -- icerigi KAP'tan yeniden uretilebilen
    bir onbellek, veri kaybi degil.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(holdings)")}
    if cols and "section" not in cols:
        conn.execute("DROP TABLE holdings")
        conn.commit()


def last_cached_date(conn):
    row = conn.execute("SELECT MAX(date) AS d FROM prices").fetchone()
    return row["d"]


def _clean(df):
    """pandas NaN -> None so sqlite stores NULL instead of the float nan."""
    return df.astype(object).where(pd.notna(df), None)


def store_prices(conn, df):
    cols = ["fund_code", "date", "kind", "fund_name", "price",
            "shares_outstanding", "investor_count", "portfolio_size"]
    rows = _clean(df[cols]).itertuples(index=False, name=None)
    conn.executemany(
        f"INSERT OR REPLACE INTO prices ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        rows,
    )
    conn.commit()


def store_breakdown(conn, df):
    pct_cols = [c for c in df.columns if c.endswith("_pct")]
    rows = []
    for r in df.to_dict("records"):
        alloc = {c: round(r[c], 2) for c in pct_cols if pd.notna(r[c]) and r[c]}
        if alloc:
            rows.append((r["fund_code"], r["date"], json.dumps(alloc)))
    conn.executemany("INSERT OR REPLACE INTO breakdown VALUES (?,?,?)", rows)
    conn.commit()


def sync(days=None, kinds=KINDS, conn=None, log=print):
    """Fetch TEFAS into the cache.

    days=N seeds the last N days; days=None pulls only what is missing since the
    last cached date (the daily incremental run). Breakdown is fetched for the
    end date only -- the detail view shows current allocation, not its history.
    """
    conn = conn or connect()
    end = date.today()
    if days is not None:
        start = end - timedelta(days=days)
    else:
        last = last_cached_date(conn)
        if not last:
            raise RuntimeError("Cache bos. Once --days ile seed calistirin.")
        start = date.fromisoformat(last) + timedelta(days=1)
        if start > end:
            log("Guncel, yapilacak is yok.")
            return 0

    crawler = Crawler()
    total = 0
    for kind in kinds:
        log(f"{kind}: {start} -> {end} fiyat verisi cekiliyor...")
        df = crawler.fetch(start, end, kind=kind, columns="info")
        if not df.empty:
            store_prices(conn, df)
            total += len(df)
        log(f"{kind}: {len(df)} satir")

    # Breakdown ignores fund_code in pytefas 0.4.1, so one bulk call per kind.
    for kind in kinds:
        log(f"{kind}: portfoy dagilimi cekiliyor...")
        df = crawler.fetch(end - timedelta(days=7), end, kind=kind, columns="breakdown")
        if not df.empty:
            store_breakdown(conn, df.sort_values("date").drop_duplicates("fund_code", keep="last"))

    # KAP is a separate source; a failure there must not lose the TEFAS work.
    try:
        kap.sync(conn, days=(days if days is not None else 14), log=log)
    except kap.KapError as exc:
        log(f"KAP atlandi: {exc}")

    log(f"Bitti. {total} fiyat satiri islendi.")
    return total


if __name__ == "__main__":
    days = None
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    sync(days=days)
