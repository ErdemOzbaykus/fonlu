"""Supabase Postgres onbellegi. Ayrica seed/update CLI: python -m fonlu.store --days 90"""

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pytefas import Crawler

from . import kap

SCHEMA_SQL = (Path(__file__).resolve().parent / "schema.sql").read_text()
KINDS = ("YAT", "EMK", "BYF")

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    """Surec basina tek havuz. Ilk kullanimda aciliyor: import aninda acmak
    DATABASE_URL'i her import edende zorunlu kilardi (testler, gocmen script)."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool


def create_schema(conn):
    """schema.sql'i baglantinin search_path'indeki semaya kurar. Uretimde sema
    migration'la kuruluyor; bu yol testler ve gocmen script icin."""
    conn.execute(SCHEMA_SQL)
    conn.commit()


def last_cached_date(conn):
    """Onbellekteki en son fiyat gunu. Artik `date` nesnesi donuyor, metin degil."""
    return conn.execute("SELECT MAX(date) AS d FROM prices").fetchone()["d"]


def _clean(df):
    """pandas NaN -> None, sqlite yerine artik psycopg NULL yazsin diye."""
    return df.astype(object).where(pd.notna(df), None)


def store_prices(conn, df):
    cols = ["fund_code", "date", "kind", "fund_name", "price",
            "shares_outstanding", "investor_count", "portfolio_size"]
    # date sutunu artik gercek `date` tipinde; pytefas metin de verse
    # Timestamp da verse tek bicime indiriyoruz.
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    rows = list(_clean(df[cols]).itertuples(index=False, name=None))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols[2:])
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO prices ({','.join(cols)})"
            f" VALUES ({','.join(['%s'] * len(cols))})"
            f" ON CONFLICT (fund_code, date) DO UPDATE SET {updates}",
            rows,
        )
    conn.commit()


def store_breakdown(conn, df):
    pct_cols = [c for c in df.columns if c.endswith("_pct")]
    rows = []
    for r in df.to_dict("records"):
        alloc = {c: round(r[c], 2) for c in pct_cols if pd.notna(r[c]) and r[c]}
        if alloc:
            rows.append((r["fund_code"], pd.to_datetime(r["date"]).date(), json.dumps(alloc)))
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO breakdown (fund_code, date, allocation) VALUES (%s, %s, %s)"
            " ON CONFLICT (fund_code) DO UPDATE SET"
            " date = EXCLUDED.date, allocation = EXCLUDED.allocation",
            rows,
        )
    conn.commit()


def sync(days=None, kinds=KINDS, conn=None, log=print):
    """Fetch TEFAS into the cache.

    days=N seeds the last N days; days=None pulls only what is missing since the
    last cached date (the daily incremental run). Breakdown is fetched for the
    end date only -- the detail view shows current allocation, not its history.
    """
    if conn is None:
        with get_pool().connection() as conn:
            return _sync(conn, days, kinds, log)
    return _sync(conn, days, kinds, log)


def _sync(conn, days, kinds, log):
    end = date.today()
    if days is not None:
        start = end - timedelta(days=days)
    else:
        last = last_cached_date(conn)
        if not last:
            raise RuntimeError("Cache bos. Once --days ile seed calistirin.")
        start = last + timedelta(days=1)   # last artik date; fromisoformat gerekmiyor
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
