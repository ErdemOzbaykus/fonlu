"""Fonlu API. TEFAS reads are served from the Postgres cache; only /api/refresh
talks to TEFAS, and it does so in the background."""

import calendar
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Annotated, Literal, Optional

import psycopg
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from pytefas import TefasAPIError, TefasInvalidParameterError, TefasRateLimitError

from . import auth, holdings, kap, kiid, store

# Piyasa ve KAP saatleri Turkiye'ye gore; konteyner UTC'de kosuyor.
TZ = ZoneInfo("Europe/Istanbul")
SYNC_HOUR = 10          # tam senkron saati (10:05)


@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=_scheduler, daemon=True).start()
    yield


# Uygulama public bir adreste; API yuzeyini disariya haritalatmaya gerek yok.
app = FastAPI(
    title="Fonlu API",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# Tarama yaniti sikistirilmadan ~1.2 MB; gzip'le ~10'da birine iniyor. Telefon
# baglantisinda listenin gec gelmesinin en buyuk sebebi buydu.
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Env eksikse ilk istekte KeyError -> govdesi JSON olmayan 500 doner ve
# frontend "is not valid JSON" der. Baslangicta patlasin, sebep loglarda gorunsun.
for _var in ("DATABASE_URL", "SUPABASE_URL"):
    if not os.environ.get(_var):
        raise RuntimeError(f"{_var} tanimli degil: .env dosyasini konteynere verin.")

STATIC = Path(__file__).resolve().parent.parent / "static"

log_ = logging.getLogger(__name__)

refresh_state = {"running": False, "log": [], "error": None}


def _run_sync(days=None, kap_only=False):
    """Hem /api/refresh'in hem zamanlayicinin kullandigi tek senkron yolu."""
    if refresh_state["running"]:
        return
    refresh_state.update(running=True, log=[], error=None)
    log = refresh_state["log"].append
    try:
        if kap_only:
            with store.get_pool().connection() as conn:
                kap.sync(conn, days=1, log=log)
        else:
            store.sync(days=days, log=log)
        # KAP adimi yeni portfoy dagilim raporu getirmis olabilir; bir kez
        # ayiklanmis fonlarin kalemleri el degmeden guncellensin.
        with store.get_pool().connection() as conn:
            refresh_holdings(conn, log=log)
    except Exception:
        # Ham istisna metni DATABASE_URL'i (dolayisiyla parolayi) icerebiliyor;
        # ayrinti loglarda kalsin, istemciye sabit mesaj gitsin.
        log_.exception("Senkronizasyon basarisiz")
        refresh_state["error"] = "Senkronizasyon basarisiz, loglara bakin"
    finally:
        refresh_state["running"] = False


def _next_wake(now: datetime) -> datetime:
    """Bir sonraki saat basi + 5 dakika."""
    nxt = now.replace(minute=5, second=0, microsecond=0)
    return nxt if nxt > now else nxt + timedelta(hours=1)


def _scheduler():
    """Her saatin 5'inde uyanir: 10:05'te tam senkron, diger saatlerde yalniz KAP.

    KAP'in push/websocket ucu yok, "dinleyici" ancak yoklama olabiliyor; saatlik
    KAP adimi bildirim kutusunu gun icinde guncel tutan sey.
    ponytail: surec ici zamanlayici, tek uvicorn iscisi varsayiyor. `--workers`
    verilirse her isci ayri tetikler; o noktada isi konteyner disina, cron'a tasi.
    """
    while True:
        now = datetime.now(TZ)
        nxt = _next_wake(now)
        # sleep yerine Event().wait: sinyal aldiginda beklemeyi bolmesi icin.
        threading.Event().wait((nxt - now).total_seconds())
        _run_sync(kap_only=nxt.hour != SYNC_HOUR)

# Onbellek bir donemin baslangicina tam yetismedginde bu kadar gunluk sapma
# donemi gecersiz saymaz (hafta sonu / tatil).
GRACE_DAYS = 7


def _months_back(d: date, months: int) -> date:
    """'1 ay once' = onceki ayin ayni gunu, 30 takvim gunu degil.

    TEFAS donemleri takvim ayi olarak hesapliyor; 30 gun geriye gitmek ozellikle
    31 gunluk aylarda ankoru kaydirip getiriyi yanlis gosteriyordu.
    """
    total = d.year * 12 + (d.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def db():
    with store.get_pool().connection() as conn:
        yield conn


Db = Annotated[psycopg.Connection, Depends(db)]
User = Annotated[str, Depends(auth.current_user)]


@app.exception_handler(TefasRateLimitError)
def _rate_limit(request, exc):
    return JSONResponse({"detail": "TEFAS hiz limiti asildi, birazdan tekrar deneyin."}, 429)


@app.exception_handler(TefasInvalidParameterError)
def _bad_param(request, exc):
    return JSONResponse({"detail": f"Gecersiz TEFAS parametresi: {exc}"}, 400)


@app.exception_handler(TefasAPIError)
def _api_error(request, exc):
    log_.warning("TEFAS API hatasi: %s", exc)
    return JSONResponse({"detail": "TEFAS su an yanit vermiyor."}, 502)


def _range(start: Optional[str], end: Optional[str]) -> tuple[str, str]:
    end = end or date.today().isoformat()
    start = start or (date.fromisoformat(end) - timedelta(days=30)).isoformat()
    if start > end:
        raise HTTPException(400, "Baslangic tarihi bitisten sonra olamaz.")
    return start, end


@app.get("/api/status")
def status(conn: Db, user: User):
    # Dort ayri sorgu dort gidis-donus demekti; sayimlar tek turda gelsin.
    row = conn.execute(
        """SELECT (SELECT COUNT(*) FROM prices) AS rows,
                  (SELECT COUNT(DISTINCT fund_code) FROM prices) AS funds,
                  (SELECT COUNT(*) FROM kap_disclosures) AS kap,
                  (SELECT MAX(date) FROM prices) AS last_date"""
    ).fetchone()
    return {**row, "refresh": refresh_state}


@app.post("/api/refresh")
def refresh(tasks: BackgroundTasks, user: User,
            days: Optional[int] = Query(None, ge=1, le=730)):
    if refresh_state["running"]:
        raise HTTPException(409, "Guncelleme zaten calisiyor.")
    # ponytail: kendi baglantisini havuzdan alsin; istek kapsamindaki baglanti
    # bu noktada havuza geri verilmis oluyor.
    tasks.add_task(_run_sync, days)
    return {"status": "started"}


# ponytail: surec ici onbellek, dolu olunca komple bosalir -- LRU degil, ama
# anahtar zaten avuc dolusu (donem x tur x arama). lru_cache kullanilamadi:
# `conn` anahtara girer, her istek yeni baglantiyla gelince onbellek ise yaramazdi.
# Girdi basina ~5 MB; dar bellekte _SCAN_MAX'i dusur.
_SCAN_MAX = 8
_scan_cache: dict = {}


def _scan(conn, stamp: date, start: str, end: str, kind: Optional[str], q: Optional[str],
          wanted: Optional[tuple[str, ...]]):
    """Tarama sorgusunun ham satirlari + donem oncesi ankor fiyatlari.

    Fiyatlar gunde bir kez /api/refresh ile degisiyor; anahtardaki `stamp`
    (onbellegin son fiyat gunu) degisince girdiler kendiliginden gecersiz kalir.
    Doner degerler okunur kabul edilir: cagiran her fon icin yeni sozluk kuruyor.
    """
    key = (stamp, start, end, kind, q, wanted)
    if key in _scan_cache:
        return _scan_cache[key]

    code_filter = (" AND fund_code IN (%s)" % ",".join(["%s"] * len(wanted))) if wanted else ""
    rows = conn.execute(
        """
        SELECT p.fund_code, p.kind, p.fund_name, p.last_price, p.first_price,
               p.prev_price, p.portfolio_size, p.investor_count, p.first_date,
               p.last_date, p.points, b.allocation
        FROM (
            SELECT fund_code, kind, fund_name, portfolio_size, investor_count,
                   price AS last_price, date AS last_date,
                   FIRST_VALUE(price) OVER w AS first_price,
                   FIRST_VALUE(date)  OVER w AS first_date,
                   -- Gunluk getiri icin bir onceki islem gununun fiyati. LAG cerceve
                   -- (ROWS BETWEEN ...) tanimini yok sayar, w'nin siralamasini kullanir.
                   LAG(price) OVER w AS prev_price,
                   COUNT(*) OVER (PARTITION BY fund_code) AS points,
                   ROW_NUMBER() OVER (PARTITION BY fund_code ORDER BY date DESC) AS rn
            FROM prices
            WHERE date BETWEEN %s AND %s
              -- ::text sart: Postgres ciplak bir parametrenin tipini IS NULL
              -- icinde cikaramiyor, "could not determine data type" diyor.
              AND (%s::text IS NULL OR kind = %s)
              AND (%s::text IS NULL OR fund_code LIKE %s OR UPPER(fund_name) LIKE %s)
              """ + code_filter + """
            WINDOW w AS (PARTITION BY fund_code ORDER BY date
                         ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)
        ) p
        LEFT JOIN breakdown b ON b.fund_code = p.fund_code
        WHERE p.rn = 1
        """,
        (start, end, kind, kind, q, f"%{(q or '').upper()}%", f"%{(q or '').upper()}%",
         *(wanted or [])),
    ).fetchall()

    # Donem getirisi, aralik ICINDEKI ilk fiyattan degil, baslangictan onceki son
    # fiyattan hesaplanmali: 17 Mayis Pazar ise TEFAS 15 Mayis kapanisini esas alir,
    # aralik icindeki ilk fiyati (18 Mayis) almak donemi kisaltip getiriyi bozuyor.
    # Tatil bosluklari icin 15 gunluk pencere yetiyor.
    base = {r["fund_code"]: r for r in conn.execute(
        """SELECT fund_code, price, date FROM (
             SELECT fund_code, price, date,
                    ROW_NUMBER() OVER (PARTITION BY fund_code ORDER BY date DESC) rn
             FROM prices WHERE date BETWEEN %s::date - 15 AND %s) t
           WHERE rn = 1""", (start, start))}

    if len(_scan_cache) >= _SCAN_MAX:
        _scan_cache.clear()
    _scan_cache[key] = (rows, base)
    return rows, base


@app.get("/api/funds")
def list_funds(
    conn: Db,
    user: User,
    kind: Optional[Literal["YAT", "EMK", "BYF", "GYF", "GSYF"]] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    q: Optional[str] = None,
    codes: Optional[str] = Query(None, description="Virgulle ayrilmis fon kodlari"),
    category: Optional[str] = None,
    unvan: Optional[str] = None,
    fon_turu: Optional[str] = None,
    min_return: Optional[float] = None,
    max_return: Optional[float] = None,
    min_size: Optional[float] = None,
    limit: int = Query(3000, ge=1, le=5000),
):
    """Funds with their return over [start, end], best return first."""
    start, end = _range(start, end)
    # `codes` takip listesi gibi sabit bir kume icin: diger filtreler uygulanmaz,
    # yoksa kaydedilen fon tarama filtresine takilip listeden dusuyor.
    wanted = [c.strip().upper() for c in codes.split(",") if c.strip()] if codes else None
    if wanted is not None and not wanted:
        return {"start": start, "end": end, "count": 0,
                "categories": CATEGORIES + ["Karma"], "unvanlar": UNVANLAR,
                "turler": TURLER, "funds": []}
    if wanted:
        # "Tam olarak bu fonlar" demek; arama/tip filtreleri de gecersiz kalmali,
        # yoksa istenen kod SQL tarafinda elenip sessizce bos donuyor.
        kind = q = None
    rows, base = _scan(conn, store.last_cached_date(conn), start, end, kind, q,
                       tuple(wanted) if wanted else None)

    out = []
    for r in rows:
        # Fonun aralik oncesi fiyati yoksa (yeni ihrac) aralik ici ilk fiyat kalir.
        anchor = base.get(r["fund_code"])
        first_price = anchor["price"] if anchor else r["first_price"]
        first_date = anchor["date"] if anchor else r["first_date"]
        ret = _pct(first_price, r["last_price"])
        # allocation jsonb: psycopg zaten dict olarak veriyor, json.loads gerekmiyor.
        groups = _groups(r["allocation"] or {})
        cat = _category(groups)
        unv = _unvan(r["fund_name"])
        if not wanted:  # sabit kume istendiginde tarama filtreleri uygulanmaz
            if min_return is not None and (ret is None or ret < min_return):
                continue
            if max_return is not None and (ret is None or ret > max_return):
                continue
            if min_size is not None and (r["portfolio_size"] or 0) < min_size:
                continue
            if category and cat != category:
                continue
            if unvan and unvan not in unv:
                continue
            if fon_turu and fon_turu not in (FON_TURU.get(t) for t in unv):
                continue
        fund = {k: r[k] for k in r.keys() if k != "allocation"}
        out.append({**fund, "first_price": first_price, "first_date": first_date,
                    "return_pct": ret, "category": cat, "unvan": ", ".join(unv), "groups": groups,
                    # Aralikta tek fiyat varsa onceki gun yok; None kalir, arayuz "—" basar.
                    "daily_pct": _pct(r["prev_price"], r["last_price"])})
    out.sort(key=lambda f: (f["return_pct"] is None, -(f["return_pct"] or 0)))
    return {"start": start, "end": end, "count": len(out),
            "categories": CATEGORIES + ["Karma"], "unvanlar": UNVANLAR,
            "turler": TURLER, "funds": out[:limit]}


def _pct(first, last):
    if not first or last is None:
        return None
    return round((last / first - 1) * 100, 2)


def _risk(prices: list[float]) -> dict:
    """Annualised volatility and worst peak-to-trough drop over the series.

    ponytail: plain sample stddev of daily returns x sqrt(252). Enough to rank
    funds against each other; not a substitute for TEFAS's official risk grade,
    which pytefas does not expose.
    """
    rets = [b / a - 1 for a, b in zip(prices, prices[1:]) if a]
    if len(rets) < 5:
        return {"volatility_pct": None, "max_drawdown_pct": None}
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    peak, mdd = prices[0], 0.0
    for p in prices:
        peak = max(peak, p)
        if peak:
            mdd = min(mdd, p / peak - 1)
    return {
        "volatility_pct": round(var**0.5 * (252**0.5) * 100, 2),
        "max_drawdown_pct": round(mdd * 100, 2),
    }


# Coarse buckets so a fund can be filtered by what it actually holds. TEFAS
# encodes this in the fund title; deriving it from the breakdown is more honest.
CATEGORY_RULES = [
    ("Hisse Senedi", ("stock_pct", "foreign_stock_pct")),
    ("Kıymetli Maden", ("precious_metals_pct", "precious_metals_etf_pct",
                        "precious_metals_government_debt_pct", "deposit_gold_pct")),
    ("Para Piyasası", ("repo_pct", "reverse_repo_pct", "takasbank_money_market_pct",
                       "bist_money_market_pct", "term_deposit_pct", "deposit_tl_pct",
                       "participation_account_tl_pct")),
    ("Borçlanma Aracı", ("government_bond_pct", "treasury_bill_pct", "private_sector_bond_pct",
                         "financing_bill_pct", "bank_bill_pct", "eurobond_pct",
                         "asset_backed_securities_pct", "government_lease_certificate_tl_pct")),
    ("Döviz", ("deposit_fx_pct", "fx_payable_bond_pct", "fx_payable_bill_pct",
               "government_external_debt_pct", "private_sector_external_debt_pct")),
    ("Fon Sepeti", ("investment_fund_pct", "etf_pct", "fund_participation_certificate_pct",
                    "foreign_etf_pct")),
]
CATEGORIES = [name for name, _ in CATEGORY_RULES]


# Fon unvan turu: TEFAS'in "Fon Unvan Turu" filtresindeki degerler. Bunlar
# semsiye fon turu degil, unvanda gecen etiketler -- bir fon birden fazlasina
# girebiliyor ("... KATILIM HISSE SENEDI ..."), o yuzden liste donuyor.
# pytefas ayri bir sutun vermedigi icin unvandan okunuyor; SPK bu ibareleri
# unvanda zorunlu tuttugundan tahmin degil, resmi etiket.
UNVAN_RULES = [
    ("Altın", r"ALTIN"),
    ("Borçlanma Araçları", r"BORÇLANMA ARAÇ"),
    ("Döviz", r"DÖVİZ"),
    ("Endeks", r"ENDEKS"),
    ("Endeks Hisse Senedi", r"ENDEKSİ? HİSSE SENEDİ"),
    ("Gümüş", r"GÜMÜŞ"),
    ("Hisse Senedi", r"HİSSE SENEDİ"),
    ("Hisse Senedi Yoğun", r"HİSSE SENEDİ YOĞUN"),
    ("Katılım", r"KATILIM"),
    ("Sürdürülebilirlik Fonları", r"SÜRDÜRÜLEB[İI]L"),
    ("Yabancı", r"YABANCI"),
]
UNVANLAR = [name for name, _ in UNVAN_RULES]


# TEFAS'in "Fon Turu" filtresi: unvan turlerinin bir alt kumesi, sadece
# ekli/eksik "Fonu" ibaresiyle yaziliyor. Ayri regex listesi tutulmuyor,
# ikisi ayrisirsa iki yerde duzeltme gerekirdi.
FON_TURU = {"Altın": "Altın Fonu", "Endeks": "Endeks Fon", "Gümüş": "Gümüş Fonu",
            "Hisse Senedi": "Hisse Senedi Fonu", "Hisse Senedi Yoğun": "Hisse Senedi Yoğun",
            "Yabancı": "Yabancı Fon"}
TURLER = list(FON_TURU.values())


def _unvan(fund_name: Optional[str]) -> list:
    """Unvanda gecen tum etiketler. Parantezler KORUNUYOR: "(HISSE SENEDI
    YOGUN FON)" TEFAS'ta bir unvan turu, atilirsa etiket kaybolur."""
    if not fund_name:
        return []
    name = fund_name.upper()
    return [lab for lab, pat in UNVAN_RULES if re.search(pat, name)]


def _groups(allocation: dict) -> dict:
    """54 pct columns collapsed to the 6 buckets, plus whatever is left over."""
    if not allocation:
        return {}
    out = {n: round(sum(allocation.get(k, 0) for k in keys), 2) for n, keys in CATEGORY_RULES}
    out["Diğer"] = round(max(0.0, sum(allocation.values()) - sum(out.values())), 2)
    # Keep negatives: a leveraged fund reports a short money-market leg (e.g.
    # +123.7 stock / -23.7 BIST money market) and dropping it hides the leverage.
    return {k: v for k, v in out.items() if v}


def _category(groups: dict) -> Optional[str]:
    if not groups:
        return None
    best = max(groups, key=groups.get)
    return best if groups[best] >= 25 else "Karma"



def _peer(conn, meta, alloc) -> Optional[dict]:
    """Fonun kendi sinifi icindeki buyukluk payi ve sirasi.

    Sinif = ayni `kind` + ayni dagilim kategorisi (Hisse Senedi, Para Piyasasi...);
    `kind` tek basina cok genis, TEFAS ayrica bir fon turu sutunu vermiyor.
    Payda fonun son fiyat gunuyle kuruluyor: o gun fiyat aciklamayan fon disarida
    kalir, pay bir miktar yuksek cikar.
    """
    size, cat = meta["portfolio_size"], _category(_groups(alloc))
    if not size or not cat:
        return None
    sizes = [r["portfolio_size"] for r in conn.execute(
        "SELECT p.portfolio_size, b.allocation FROM prices p"
        " LEFT JOIN breakdown b ON b.fund_code = p.fund_code"
        " WHERE p.date = %s AND p.kind = %s AND p.portfolio_size > 0",
        (meta["date"], meta["kind"])).fetchall()
        if _category(_groups(r["allocation"] or {})) == cat]
    total = sum(sizes)
    return {"category": cat, "count": len(sizes),
            "rank": sum(1 for s in sizes if s > size) + 1,
            "share_pct": round(size / total * 100, 2) if total else None}


@app.get("/api/funds/{code}")
def fund_detail(code: str, conn: Db, user: User,
                start: Optional[str] = None, end: Optional[str] = None):
    code = code.upper()
    start, end = _range(start, end)
    series = conn.execute(
        "SELECT date, price, shares_outstanding FROM prices"
        " WHERE fund_code = %s AND date BETWEEN %s AND %s ORDER BY date",
        (code, start, end),
    ).fetchall()
    if not series:
        raise HTTPException(404, f"{code} icin onbellekte veri yok. Once /api/refresh calistirin.")
    meta = conn.execute(
        "SELECT fund_name, kind, portfolio_size, investor_count, price, date"
        " FROM prices WHERE fund_code = %s ORDER BY date DESC LIMIT 1",
        (code,),
    ).fetchone()
    bd = conn.execute("SELECT date, allocation FROM breakdown WHERE fund_code = %s", (code,)).fetchone()
    alloc = bd["allocation"] if bd else {}

    # Period returns over whatever the cache holds, independent of the chosen range.
    hist = conn.execute(
        "SELECT date, price FROM prices WHERE fund_code = %s ORDER BY date", (code,)
    ).fetchall()
    prices = [r["price"] for r in hist if r["price"]]
    last = hist[-1]["price"]
    periods = {}
    # prices.date artik gercek `date`; fromisoformat'a gerek yok.
    last_date = hist[-1]["date"]
    first_date = hist[0]["date"]
    for label, months in (("1A", 1), ("3A", 3), ("6A", 6), ("1Y", 12)):
        target = _months_back(last_date, months)
        # TEFAS'in yaptigi gibi hedef tarihteki ya da ONCESINDEKI son fiyata
        # bagla; hedef hafta sonuna denk gelirse bir sonraki islem gunune atlamak
        # donemi kisaltip getiriyi yukseltiyor.
        prior = [r for r in hist if r["date"] <= target]
        if prior:
            periods[label] = _pct(prior[-1]["price"], last)
        elif first_date <= target + timedelta(days=GRACE_DAYS):
            # Onbellek tam o gune yetismiyor ama birkac gun icinde basliyor.
            periods[label] = _pct(hist[0]["price"], last)
        else:
            periods[label] = None

    return {
        "fund_code": code,
        **dict(meta),
        "return_pct": _pct(series[0]["price"], series[-1]["price"]),
        # Son iki islem gunu arasi degisim; tek gunluk gecmiste onceki gun yok.
        "daily_pct": _pct(hist[-2]["price"], last) if len(hist) > 1 else None,
        "series": [dict(r) for r in series],
        "allocation": alloc,
        "allocation_date": bd["date"] if bd else None,
        "groups": _groups(alloc),
        "category": _category(_groups(alloc)),
        "unvan": ", ".join(_unvan(meta["fund_name"])),
        "periods": periods,
        "peer": _peer(conn, meta, alloc),
        **_risk(prices),
        "kap_count": conn.execute(
            "SELECT COUNT(*) c FROM kap_disclosures WHERE fund_code = %s", (code,)
        ).fetchone()["c"],
    }


@app.get("/api/funds/{code}/kap")
def fund_kap(code: str, conn: Db, user: User, limit: int = Query(50, ge=1, le=200)):
    """Fonun KAP bildirimleri (onbellekten). Detayli portfoy kirilimi 'Portfoy
    Dagilim Raporu' bildirimlerinin PDF ekinde bulunur."""
    rows = conn.execute(
        "SELECT * FROM kap_disclosures WHERE fund_code = %s ORDER BY publish_date DESC LIMIT %s",
        (code.upper(), limit),
    ).fetchall()
    return {
        "fund_code": code.upper(),
        "disclosures": [
            {**dict(r), "url": kap.DISCLOSURE_URL.format(r["disclosure_index"])} for r in rows
        ],
    }


@app.get("/api/kap/{disclosure_index}/attachments")
def kap_attachments(disclosure_index: int, user: User):
    """Bir bildirimin PDF ekleri. KAP'a canli gider, sadece tiklaninca cagrilir."""
    try:
        atts = kap.attachments(disclosure_index)
    except kap.KapError as exc:
        raise HTTPException(502, str(exc))
    # KAP'in kendi linki PDF'i Java-serialize sarmalayici icinde donduruyor, o
    # yuzden dosyayi /api/kap/file/{obj_id} ucundan gecirip temiz PDF veriyoruz.
    return {"attachments": atts}


@app.get("/api/kap/file/{obj_id}")
def kap_file(obj_id: str, user: User):
    """KAP ekini temiz PDF olarak servis eder."""
    if not obj_id.isalnum():
        raise HTTPException(400, "Gecersiz dosya kimligi.")
    try:
        pdf = holdings.fetch_pdf(obj_id)
    except kap.KapError as exc:
        raise HTTPException(502, str(exc))
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{obj_id}.pdf"'})


@app.get("/api/funds/{code}/form")
def fund_form(code: str, conn: Db, user: User):
    """Fonun son Yatirimci Bilgi Formundan stopaj, yonetim ucreti ve alim valoru.

    Ayri uc: PDF indirip ayristirmak birkac saniye suruyor, detay yaniti
    bunu beklememeli. Arayuz cekmeceyi cizdikten sonra cagiriyor.
    """
    row = conn.execute(
        "SELECT disclosure_index, publish_date FROM kap_disclosures"
        " WHERE fund_code = %s AND subject = %s AND attachment_count > 0"
        " ORDER BY publish_date DESC LIMIT 1", (code.upper(), kiid.SUBJECT),
    ).fetchone()
    if not row:
        raise HTTPException(404, f"{code.upper()} icin ekli bir yatirimci bilgi formu yok.")
    try:
        fields = kiid.fetch(row["disclosure_index"])
    except kap.KapError as exc:
        raise HTTPException(502, str(exc))
    return {**fields, "form_date": row["publish_date"][:10],
            "disclosure_index": row["disclosure_index"]}


def _section_totals(alloc: dict) -> dict:
    """TEFAS dagilim sutunlarini PDF bolum anahtarlarina toplar."""
    out = {}
    for key, value in alloc.items():
        if section := holdings.TEFAS_TO_SECTION.get(key):
            out[section] = round(out.get(section, 0) + value, 2)
    return out


def _match(tefas_pct: float, extracted_pct: float) -> str:
    """Cikarimin TEFAS'la tutarliligi.

    PDF bolum adlari TEFAS kategorileriyle birebir ortusmuyor (bir raporda
    'KIRA SERTIFIKALARI' basligi altinda duran kalem TEFAS'ta mevduat sayilabiliyor),
    bu yuzden bir bolum ancak sayilar tuttugunda kalem detayi olarak sunuluyor.
    """
    if not extracted_pct:
        return "yok"
    if not tefas_pct:
        return "eslesmedi"
    diff = abs(extracted_pct - tefas_pct)
    if diff <= max(3.0, abs(tefas_pct) * 0.15):
        return "tam"
    if abs(extracted_pct) < abs(tefas_pct):
        return "kismi"
    return "eslesmedi"


@app.get("/api/funds/{code}/holdings")
def fund_holdings(code: str, conn: Db, user: User):
    """Fonun kalem bazli portfoyu, varlik sinifina gore gruplanmis.

    Her bolum icin TEFAS'in bildirdigi yuzde ile cikarilan yuzde birlikte
    donuyor; arayuz bir dagilim satirini ancak ikisi tutuyorsa detaya aciyor.
    Iki rapor varsa kalemler bir onceki ayla kiyaslanip `delta`/`status`
    aliyor -- varlik yeni mi girdi, cikti mi, agirligi degisti mi.
    """
    code = code.upper()
    dates = [r["report_date"] for r in conn.execute(
        "SELECT DISTINCT report_date FROM holdings WHERE fund_code = %s"
        " ORDER BY report_date DESC LIMIT 2", (code,)).fetchall()]
    bd = conn.execute("SELECT allocation FROM breakdown WHERE fund_code = %s", (code,)).fetchone()
    tefas = _section_totals(bd["allocation"] if bd else {})
    if not dates:
        return {"fund_code": code, "reports": [], "sections": {},
                "tefas_sections": tefas, "labels": holdings.SECTION_LABELS,
                "key_to_section": holdings.TEFAS_TO_SECTION}

    current, previous = dates[0], (dates[1] if len(dates) > 1 else None)
    rows = conn.execute(
        "SELECT section, code, isin, issuer, value, weight_pct FROM holdings"
        " WHERE fund_code = %s AND report_date = %s ORDER BY weight_pct DESC",
        (code, current)).fetchall()
    # previous yoksa NULL gecilir; report_date artik `date`, bos metin tip hatasi verir.
    prev = {(r["section"], r["code"]): r["weight_pct"] for r in conn.execute(
        "SELECT section, code, weight_pct FROM holdings WHERE fund_code = %s AND report_date = %s",
        (code, previous))}

    sections: dict[str, dict] = {}
    for r in rows:
        item = dict(r)
        was = prev.pop((r["section"], r["code"]), None)
        item["prev_weight_pct"] = was
        item["delta"] = None if was is None else round(r["weight_pct"] - was, 2)
        item["status"] = "yeni" if previous and was is None else "mevcut"
        sections.setdefault(r["section"], {"items": []})["items"].append(item)

    # Gecen ay olup bu ay portfoyde olmayan kalemler de gorunsun.
    for (section, item_code), was in prev.items():
        sections.setdefault(section, {"items": []})["items"].append({
            "section": section, "code": item_code, "isin": "", "issuer": "",
            "value": 0.0, "weight_pct": 0.0, "prev_weight_pct": was,
            "delta": round(-was, 2), "status": "cikti",
        })

    for section, data in sections.items():
        extracted = round(sum(i["weight_pct"] for i in data["items"]), 2)
        data["extracted_pct"] = extracted
        data["tefas_pct"] = tefas.get(section)
        data["match"] = _match(tefas.get(section, 0), extracted)
        data["label"] = holdings.SECTION_LABELS.get(section, section)
        data["items"].sort(key=lambda i: -i["weight_pct"])

    return {
        "fund_code": code,
        "reports": dates,
        "current_report": current,
        "previous_report": previous,
        "sections": sections,
        "tefas_sections": tefas,
        "labels": holdings.SECTION_LABELS,
        # Arayuz hangi dagilim satirinin hangi bolume actigini bilsin.
        "key_to_section": holdings.TEFAS_TO_SECTION,
    }


def _extract_holdings(conn, code: str) -> int:
    """Son iki 'Portfoy Dagilim Raporu' PDF'ini indirip kalemleri yazar.

    Iki rapor cekiliyor cunku ay bazli kiyas (varlik yeni mi, agirligi degisti mi)
    ancak onceki ayin raporu elde varsa yapilabiliyor. Senkron calisir (20-40 sn).
    Hem kullanicinin tetikledigi POST hem de senkron sonrasi otomatik tazeleme
    buradan geciyor.
    """
    reports = conn.execute(
        "SELECT disclosure_index, publish_date FROM kap_disclosures"
        " WHERE fund_code = %s AND subject LIKE 'Portföy Dağılım%%' AND attachment_count > 0"
        " ORDER BY publish_date DESC, disclosure_index DESC LIMIT 3", (code,)
    ).fetchall()
    if not reports:
        raise HTTPException(404, f"{code} icin ekli bir KAP portfoy dagilim raporu yok.")

    stored, seen = 0, set()
    for report in reports:
        report_date = report["publish_date"][:10]
        if report_date in seen:
            continue   # ayni gun ikinci rapor: yenisi yazildi, eskisi ustune yazmasin
        if len(seen) == 2:
            break      # iki ayri gun yeter; LIMIT 3 sadece ayni gun tekrarina karsi
        seen.add(report_date)
        try:
            atts = kap.attachments(report["disclosure_index"])
            parsed = holdings.parse(holdings.fetch_pdf(atts[0]["obj_id"])) if atts else []
        except kap.KapError as exc:
            # Onceki ay okunamazsa kiyas kaybolur ama guncel rapor yine degerli.
            if len(seen) == 1:
                raise HTTPException(502, str(exc))
            break
        conn.execute("DELETE FROM holdings WHERE fund_code = %s AND report_date = %s",
                     (code, report_date))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO holdings (fund_code,report_date,section,code,isin,issuer,"
                "value,weight_pct,disclosure_index) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                [(code, report_date, h["section"], h["code"], h["isin"], h["issuer"],
                  h["value"], h["weight_pct"], report["disclosure_index"]) for h in parsed],
            )
        stored += len(parsed)
    conn.commit()
    return stored


def _stale_holdings(conn) -> list[str]:
    """Bir kez ayiklanmis ama sonrasinda yeni rapor yayinlanmis fonlarin kodlari.

    publish_date metin ve KAP bazen beklenmedik bir bicim gonderiyor (kap._iso
    cevirmeyi basaramazsa ham degeri sakliyor); ::date cast'i boyle tek bir satirda
    tum senkronu dusurdugu icin once bicim suzuluyor, kiyas ISO onekiyle metin
    uzerinden yapiliyor. Ayni gun ikinci rapor gelirse disclosure_index ayirt ediyor.
    """
    return [r["fund_code"] for r in conn.execute(
        "SELECT h.fund_code FROM (SELECT DISTINCT ON (fund_code) fund_code,"
        "   to_char(report_date, 'YYYY-MM-DD') AS md, disclosure_index AS mi"
        "   FROM holdings ORDER BY fund_code, report_date DESC, disclosure_index DESC) h"
        " JOIN kap_disclosures k ON k.fund_code = h.fund_code"
        " WHERE k.subject LIKE 'Portföy Dağılım%%' AND k.attachment_count > 0"
        "   AND k.publish_date ~ '^\\d{4}-\\d{2}-\\d{2}'"
        "   AND (LEFT(k.publish_date, 10) > h.md"
        "        OR (LEFT(k.publish_date, 10) = h.md"
        "            AND k.disclosure_index > COALESCE(h.mi, 0)))"
        " GROUP BY h.fund_code ORDER BY h.fund_code").fetchall()]


def refresh_holdings(conn, log=print) -> int:
    """Yeni portfoy dagilim raporu gelen fonlarin kalemlerini yeniden ayiklar.

    Kullanici bir fonu bir kez ayikladiginda o fon takipte sayiliyor; sonraki
    aylarda KAP raporu geldiginde el degmeden guncelleniyor. Bir fonda hata
    olursa digerleri devam eder.
    """
    codes = _stale_holdings(conn)
    if codes:
        log(f"Kalem tazeleme: {len(codes)} fon ({', '.join(codes[:10])})")
    done = 0
    for code in codes:
        try:
            _extract_holdings(conn, code)
            done += 1
        except Exception as exc:   # HTTPException dahil: tek fon tum turu bozmasin
            conn.rollback()
            log(f"{code}: kalem tazeleme basarisiz -- {exc}")
    return done


@app.post("/api/funds/{code}/holdings")
def extract_holdings(code: str, conn: Db, user: User):
    """Kullanicinin bilerek tetikledigi tek fonluk ayiklama."""
    code = code.upper()
    if not _extract_holdings(conn, code):
        raise HTTPException(
            422,
            "Rapor okundu ama kalem çıkarılamadı — bu kurucunun PDF düzeni "
            "destekleniyor değil. Raporu KAP'tan açıp inceleyebilirsiniz.",
        )
    return fund_holdings(code, conn, user)


@app.get("/api/watchlist")
def get_watchlist(conn: Db, user: User):
    rows = conn.execute("SELECT fund_code FROM watchlist WHERE user_id = %s"
                        " ORDER BY added_at DESC", (user,)).fetchall()
    return {"codes": [r["fund_code"] for r in rows]}


@app.get("/api/notifications")
def notifications(conn: Db, user: User, limit: int = Query(50, ge=1, le=200)):
    """Takip listesindeki ve portfoydeki fonlarin son KAP bildirimleri.

    Okundu bilgisi sunucuda tutulmuyor: tarayicidaki son gorulen bildirim
    numarasi yetiyor, kullanici basina yeni bir tablo acmaya degmez.
    """
    rows = conn.execute(
        """SELECT * FROM kap_disclosures
           WHERE fund_code IN (SELECT fund_code FROM watchlist WHERE user_id = %s
                               UNION SELECT fund_code FROM positions WHERE user_id = %s)
           ORDER BY publish_date DESC LIMIT %s""",
        (user, user, limit),
    ).fetchall()
    return {"disclosures": [{**dict(r), "url": kap.DISCLOSURE_URL.format(r["disclosure_index"])}
                            for r in rows]}


@app.put("/api/watchlist/{code}", status_code=204)
def add_watch(code: str, conn: Db, user: User):
    code = code.upper()
    if not code.isalnum() or len(code) > 10:
        raise HTTPException(400, "Gecersiz fon kodu.")
    conn.execute("INSERT INTO watchlist (user_id, fund_code) VALUES (%s, %s)"
                 " ON CONFLICT (user_id, fund_code) DO NOTHING", (user, code))
    conn.commit()


@app.delete("/api/watchlist/{code}", status_code=204)
def remove_watch(code: str, conn: Db, user: User):
    conn.execute("DELETE FROM watchlist WHERE user_id = %s AND fund_code = %s",
                 (user, code.upper()))
    conn.commit()


@app.get("/api/compare")
def compare(
    conn: Db,
    user: User,
    codes: str = Query(..., description="Virgulle ayrilmis fon kodlari"),
    start: Optional[str] = None,
    end: Optional[str] = None,
):
    """Each fund indexed to 100 at its first price in the range."""
    wanted = [c.strip().upper() for c in codes.split(",") if c.strip()][:10]
    if not wanted:
        raise HTTPException(400, "En az bir fon kodu verin.")
    start, end = _range(start, end)
    result, missing = [], []
    for code in wanted:
        rows = conn.execute(
            "SELECT date, price FROM prices WHERE fund_code = %s AND date BETWEEN %s AND %s"
            " ORDER BY date",
            (code, start, end),
        ).fetchall()
        if not rows or not rows[0]["price"]:
            missing.append(code)
            continue
        base = rows[0]["price"]
        result.append({
            "fund_code": code,
            "return_pct": _pct(base, rows[-1]["price"]),
            "series": [{"date": r["date"], "value": round(r["price"] / base * 100, 3)} for r in rows],
        })
    return {"start": start, "end": end, "funds": result, "missing": missing}


class PositionIn(BaseModel):
    fund_code: str = Field(min_length=2, max_length=10)
    units: float = Field(gt=0)
    buy_date: date
    buy_price: float = Field(gt=0)
    note: Optional[str] = Field(None, max_length=200)

    @field_validator("fund_code")
    @classmethod
    def _upper(cls, v: str) -> str:
        v = v.strip().upper()
        if not v.isalnum():
            raise ValueError("Fon kodu harf ve rakamlardan olusmali.")
        return v


@app.post("/api/positions", status_code=201)
def add_position(pos: PositionIn, conn: Db, user: User):
    # Bilinmeyen kod neredeyse her zaman yazim hatasi; kabul edilirse fiyati
    # hesaplanamayan olu bir pozisyon olusuyor ve sessizce oyle kaliyor.
    if not conn.execute("SELECT 1 FROM prices WHERE fund_code = %s LIMIT 1",
                        (pos.fund_code,)).fetchone():
        raise HTTPException(
            404, f"{pos.fund_code} önbellekte yok. Kodu kontrol edin veya veriyi güncelleyin.")
    # Postgres'te lastrowid yok; id RETURNING ile geri geliyor.
    row = conn.execute(
        "INSERT INTO positions (user_id, fund_code, units, buy_date, buy_price, note)"
        " VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
        (user, pos.fund_code, pos.units, pos.buy_date, pos.buy_price, pos.note),
    ).fetchone()
    conn.commit()
    return {"id": row["id"]}


@app.delete("/api/positions/{pos_id}", status_code=204)
def delete_position(pos_id: int, conn: Db, user: User):
    # user_id kosulu sahiplik kontrolu: baskasinin pozisyonu 404 gorunmeli.
    if not conn.execute("DELETE FROM positions WHERE id = %s AND user_id = %s",
                        (pos_id, user)).rowcount:
        raise HTTPException(404, "Pozisyon bulunamadi.")
    conn.commit()


@app.get("/api/positions")
def list_positions(conn: Db, user: User):
    rows = conn.execute(
        """
        SELECT p.*, l.price AS last_price, l.date AS last_date, l.fund_name
        FROM positions p
        -- LATERAL sart: ROW_NUMBER'li hali her portfoy acilisinda tum prices
        -- tablosunu (600k+ satir) tariyordu; boyle her pozisyon icin PK'dan
        -- tek satir okunuyor (1.0 sn -> 0.06 sn).
        LEFT JOIN LATERAL (
            SELECT fund_name, price, date FROM prices
            WHERE fund_code = p.fund_code ORDER BY date DESC LIMIT 1
        ) l ON true
        WHERE p.user_id = %s
        ORDER BY p.fund_code, p.buy_date
        """,
        (user,),
    ).fetchall()

    positions, cost_sum, value_sum = [], 0.0, 0.0
    for r in rows:
        cost = r["units"] * r["buy_price"]
        value = r["units"] * r["last_price"] if r["last_price"] else None
        cost_sum += cost
        if value is not None:
            value_sum += value
        positions.append({
            **dict(r),
            "cost": round(cost, 2),
            "value": round(value, 2) if value is not None else None,
            "profit": round(value - cost, 2) if value is not None else None,
            "profit_pct": _pct(r["buy_price"], r["last_price"]),
        })
    return {
        "positions": positions,
        "total_cost": round(cost_sum, 2),
        "total_value": round(value_sum, 2),
        "total_profit": round(value_sum - cost_sum, 2),
        "total_profit_pct": _pct(cost_sum, value_sum),
    }


class RevalidatingStatic(StaticFiles):
    """Tarayici index.html/app.js'i yeniden dogrulamadan onbellekten servis edince
    frontend duzenlemeleri gorunmuyor. 'no-cache' her istekte dogrulama zorunlu
    kiliyor; ETag ayni kaldiginda dosya yine tekrar indirilmiyor."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", RevalidatingStatic(directory=STATIC, html=True), name="static")
