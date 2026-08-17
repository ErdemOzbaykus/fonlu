"""Self-check over the query logic, on a throwaway schema with fixed prices.
Run: docker compose --profile test run --rm tests   (no TEFAS calls)"""

import os
from datetime import date
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from fastapi.testclient import TestClient

from fonlu import auth, main, store

DSN = os.environ["DATABASE_URL"]

USER_A = "11111111-1111-4111-8111-111111111111"
USER_B = "22222222-2222-4222-8222-222222222222"
# Sozluk, cunku override lambda'si aktif kullaniciyi calisma aninda okumali:
# duz bir degiskeni yeniden atamak lambda'nin gordugu degeri degistirmez.
_who = {"id": USER_A}


def fresh_schema():
    """Her kosum kendi semasinda: testler birbirinin verisini gormesin."""
    name = "t" + uuid4().hex[:12]
    with psycopg.connect(DSN, autocommit=True) as cn:
        cn.execute(f'CREATE SCHEMA "{name}"')
    with connect_test(name) as cn:
        store.create_schema(cn)
    return name


def connect_test(schema):
    return psycopg.connect(DSN, row_factory=dict_row, options=f"-c search_path={schema}")


def _override(schema):
    """Gercek bagimlilik gibi generator olmali: duz lambda baglantiyi kapatmiyor."""
    pool = ConnectionPool(DSN, min_size=1, max_size=4, open=True,
                          kwargs={"row_factory": dict_row,
                                  "options": f"-c search_path={schema}"})

    def _db():
        with pool.connection() as conn:
            yield conn

    main.app.dependency_overrides[main.db] = _db
    main.app.dependency_overrides[auth.current_user] = lambda: _who["id"]


PRICES = [  # AAA doubles, BBB drops 20%, CCC has one day only
    ("AAA", "2026-08-10", 10.0), ("AAA", "2026-08-11", 15.0), ("AAA", "2026-08-12", 20.0),
    ("BBB", "2026-08-10", 100.0), ("BBB", "2026-08-12", 80.0),
    ("CCC", "2026-08-12", 5.0),
    # DDD trades before the window too: its return must anchor on the last price
    # at or before `start`, the way TEFAS does, not on the first price inside it.
    ("DDD", "2026-08-07", 50.0), ("DDD", "2026-08-11", 55.0), ("DDD", "2026-08-12", 60.0),
]


def build():
    schema = fresh_schema()
    with connect_test(schema) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO prices (fund_code,date,kind,fund_name,price,portfolio_size,"
                "investor_count) VALUES (%s,%s,'YAT','Test '||%s,%s,1000,10)",
                [(c, d, c, p) for c, d, p in PRICES],
            )
            cur.execute("INSERT INTO breakdown VALUES ('AAA','2026-08-12',"
                        "'{\"stock_pct\": 92.5}')")
            cur.execute("INSERT INTO breakdown VALUES ('BBB','2026-08-12',"
                        "'{\"reverse_repo_pct\": 60.0, \"deposit_tl_pct\": 40.0}')")
            cur.execute(
                "INSERT INTO kap_disclosures VALUES "
                # attachment_count 0 keeps the suite offline: extraction must bail early
                "(999,'AAA','2026-08-10 09:00:00','A FONU','Portföy Dağılım Raporu',"
                "'Temmuz','DG',0)")
        conn.commit()
    _override(schema)
    return TestClient(main.app), schema


def build_periods_client():
    """A fund whose history starts on Mon 2026-05-18 and ends Fri 2026-08-14 --
    exactly the 90-day seed shape, where the 3-month cutoff (2026-05-16) is a
    Saturday and so falls before the first price that exists."""
    from datetime import date, timedelta
    schema = fresh_schema()
    day, end, rows = date(2026, 5, 18), date(2026, 8, 14), []
    price = 10.0
    while day <= end:
        if day.weekday() < 5:  # TEFAS publishes on business days only
            rows.append(("PPP", day.isoformat(), price))
            price *= 1.001
        day += timedelta(days=1)
    with connect_test(schema) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO prices (fund_code,date,kind,fund_name,price)"
                " VALUES (%s,%s,'YAT','P',%s)", rows)
        conn.commit()

    _override(schema)
    out = TestClient(main.app).get("/api/funds/PPP").json()["periods"]
    main.app.dependency_overrides[main.db] = _restore
    return out


c, SCHEMA = build()
_restore = main.app.dependency_overrides[main.db]
R = {"start": "2026-08-01", "end": "2026-08-31"}

funds = c.get("/api/funds", params=R).json()["funds"]
by = {f["fund_code"]: f for f in funds}
assert by["AAA"]["return_pct"] == 100.0, by["AAA"]
assert by["BBB"]["return_pct"] == -20.0, by["BBB"]
assert by["CCC"]["return_pct"] == 0.0, by["CCC"]  # single point -> flat, not a crash
assert [f["fund_code"] for f in funds] == ["AAA", "DDD", "CCC", "BBB"], "return desc"

# Gunluk getiri: donem getirisinden bagimsiz, son iki islem gunu arasi.
assert by["AAA"]["daily_pct"] == 33.33, by["AAA"]   # 15 -> 20
assert by["BBB"]["daily_pct"] == -20.0, by["BBB"]   # 100 -> 80 (aradaki gun bos)
assert by["DDD"]["daily_pct"] == 9.09, by["DDD"]    # 55 -> 60
# Tek fiyati olan fonun oncesi yok: uydurulmus bir %0 yerine bos kalmali.
assert by["CCC"]["daily_pct"] is None, by["CCC"]

# range must clip the window, not just the display
win = {f["fund_code"]: f for f in c.get(
    "/api/funds", params={"start": "2026-08-11", "end": "2026-08-12"}).json()["funds"]}
assert win["AAA"]["return_pct"] == 33.33
# A start date that is itself a trading day anchors on that day's price.
assert win["DDD"]["return_pct"] == 9.09 and win["DDD"]["first_date"] == "2026-08-11"

# The real fix: when the start date has no price (weekend/holiday), fall back to
# the last price BEFORE it, the way TEFAS does. Jumping forward to the next
# trading day shortens the period and overstates the return.
gap = {f["fund_code"]: f for f in c.get(
    "/api/funds", params={"start": "2026-08-09", "end": "2026-08-12"}).json()["funds"]}
assert gap["DDD"]["first_date"] == "2026-08-07", gap["DDD"]  # not 2026-08-11
assert gap["DDD"]["return_pct"] == 20.0, gap["DDD"]          # not 9.09
# CCC has nothing before the window, so the first in-window price stays the baseline
assert gap["CCC"]["return_pct"] == 0.0

# `codes` returns exactly the requested funds, ignoring the scan's other filters —
# otherwise a saved fund vanishes from the watchlist whenever a filter is active.
saved = c.get("/api/funds", params={**R, "codes": "bbb,aaa", "min_return": 500,
                                    "category": "Kıymetli Maden", "q": "ZZZZ",
                                    "kind": "EMK"}).json()["funds"]
assert sorted(f["fund_code"] for f in saved) == ["AAA", "BBB"], saved
assert c.get("/api/funds", params={**R, "codes": " "}).json()["funds"] == []

assert [f["fund_code"] for f in c.get("/api/funds", params={**R, "min_return": 0}).json()["funds"]] == ["AAA", "DDD", "CCC"]
assert [f["fund_code"] for f in c.get("/api/funds", params={**R, "max_return": -1}).json()["funds"]] == ["BBB"]
assert [f["fund_code"] for f in c.get("/api/funds", params={**R, "q": "BB"}).json()["funds"]] == ["BBB"]
assert c.get("/api/funds", params={"start": "2026-08-31", "end": "2026-08-01"}).status_code == 400

# derived category + grouped allocation drive the table's bar and the filter
assert by["AAA"]["category"] == "Hisse Senedi", by["AAA"]
assert by["BBB"]["category"] == "Para Piyasası", by["BBB"]
assert by["BBB"]["groups"] == {"Para Piyasası": 100.0}, by["BBB"]["groups"]
assert by["CCC"]["category"] is None  # no breakdown row -> no invented category
assert [f["fund_code"] for f in c.get("/api/funds", params={**R, "category": "Hisse Senedi"}
                                      ).json()["funds"]] == ["AAA"]
assert [f["fund_code"] for f in c.get("/api/funds", params={**R, "min_size": 2000}).json()["funds"]] == []

d = c.get("/api/funds/aaa", params=R).json()
assert d["allocation"] == {"stock_pct": 92.5} and len(d["series"]) == 3 and d["price"] == 20.0
assert d["kap_count"] == 1 and d["category"] == "Hisse Senedi"
# 2 daily returns is too little to characterise risk; quote neither figure
assert d["volatility_pct"] is None and d["max_drawdown_pct"] is None
# a 90-day window on 3 days of history must not be reported as a 3-month return
assert d["periods"] == {"1A": None, "3A": None, "6A": None, "1Y": None}, d["periods"]

# A period whose cutoff lands on a weekend still counts: TEFAS publishes no price
# that day, so the first cached date is legitimately a day or two later.
per = build_periods_client()
assert per["3A"] is not None, per   # cutoff is a Saturday, data starts the Monday
assert per["1A"] is not None, per
assert per["6A"] is None and per["1Y"] is None, per  # genuinely not seeded

# "1 ay önce" is the same day of the previous month, not 30 calendar days back.
# Anchoring on 30 days shifted the baseline and pushed returns off TEFAS's figures.
assert main._months_back(date(2026, 8, 14), 1) == date(2026, 7, 14)
assert main._months_back(date(2026, 8, 14), 3) == date(2026, 5, 14)
assert main._months_back(date(2026, 8, 14), 12) == date(2025, 8, 14)
assert main._months_back(date(2026, 3, 31), 1) == date(2026, 2, 28)  # kısa ay
assert main._months_back(date(2026, 1, 15), 1) == date(2025, 12, 15)  # yıl sınırı
assert c.get("/api/funds/ZZZ", params=R).status_code == 404

kap = c.get("/api/funds/aaa/kap").json()["disclosures"]
assert len(kap) == 1 and kap[0]["url"].endswith("/Bildirim/999")
assert kap[0]["subject"] == "Portföy Dağılım Raporu"
assert c.get("/api/funds/BBB/kap").json()["disclosures"] == []

cmp = c.get("/api/compare", params={**R, "codes": "aaa,BBB,ZZZ"}).json()
assert cmp["missing"] == ["ZZZ"]
assert [p["value"] for p in cmp["funds"][0]["series"]] == [100.0, 150.0, 200.0]
assert cmp["funds"][1]["series"][-1]["value"] == 80.0

pid = c.post("/api/positions", json={"fund_code": "aaa", "units": 10,
                                     "buy_date": "2026-08-10", "buy_price": 10.0}).json()["id"]
c.post("/api/positions", json={"fund_code": "BBB", "units": 1,
                               "buy_date": "2026-08-10", "buy_price": 100.0})
p = c.get("/api/positions").json()
assert p["total_cost"] == 200.0 and p["total_value"] == 280.0
assert p["total_profit"] == 80.0 and p["total_profit_pct"] == 40.0
assert p["positions"][0]["profit_pct"] == 100.0

assert c.post("/api/positions", json={"fund_code": "AAA", "units": -1,
                                      "buy_date": "2026-08-10", "buy_price": 10.0}).status_code == 422
assert c.post("/api/positions", json={"fund_code": "A;DROP", "units": 1,
                                      "buy_date": "2026-08-10", "buy_price": 10.0}).status_code == 422
# a typo'd code used to be accepted, leaving a position that could never be valued
assert c.post("/api/positions", json={"fund_code": "ZZZZ", "units": 1,
                                      "buy_date": "2026-08-10", "buy_price": 10.0}).status_code == 404
assert c.delete(f"/api/positions/{pid}").status_code == 204
assert c.delete(f"/api/positions/{pid}").status_code == 404
assert c.get("/api/positions").json()["total_cost"] == 100.0

w = c.get("/api/watchlist").json()["codes"]
assert w == []
assert c.put("/api/watchlist/aaa").status_code == 204
assert c.put("/api/watchlist/BBB").status_code == 204
assert c.put("/api/watchlist/AAA").status_code == 204  # tekrar kaydetmek çoğaltmamalı
assert sorted(c.get("/api/watchlist").json()["codes"]) == ["AAA", "BBB"]
assert c.put("/api/watchlist/a;b").status_code == 400
assert c.delete("/api/watchlist/AAA").status_code == 204
assert c.get("/api/watchlist").json()["codes"] == ["BBB"]

# Watchlist ve pozisyonlar hesaba bagli: baska kullanici bunlari gormemeli.
_who["id"] = USER_B
assert c.get("/api/watchlist").json()["codes"] == [], "B, A'nin watchlist'ini goruyor"
assert c.put("/api/watchlist/CCC").status_code == 204
assert c.get("/api/watchlist").json()["codes"] == ["CCC"]
assert c.get("/api/positions").json()["positions"] == [], "B, A'nin pozisyonlarini goruyor"
_who["id"] = USER_A
assert c.get("/api/watchlist").json()["codes"] == ["BBB"], "A'nin listesi B'den etkilendi"
assert c.get("/api/positions").json()["total_cost"] == 100.0

h = c.get("/api/funds/AAA/holdings").json()
assert h["reports"] == [] and h["sections"] == {}
assert h["tefas_sections"] == {"hisse": 92.5}, h["tefas_sections"]
assert h["key_to_section"]["stock_pct"] == "hisse"
# BBB holds only money-market instruments, so no equity section is offered
assert c.get("/api/funds/BBB/holdings").json()["tefas_sections"] == {"repo": 60.0, "mevduat": 40.0}
# AAA's only cached disclosure has no attachment -> extraction must not be attempted
assert c.post("/api/funds/AAA/holdings").status_code == 404

# --- month-over-month diff and per-section TEFAS agreement ---
with connect_test(SCHEMA) as conn:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO holdings (fund_code,report_date,section,code,isin,issuer,"
            "value,weight_pct,disclosure_index) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", [
            # current report
            ("AAA", "2026-08-10", "hisse", "BIMAS", "TRE1", "BIM", 100.0, 50.0, 1),
            ("AAA", "2026-08-10", "hisse", "ASELS", "TRE2", "ASELSAN", 80.0, 42.5, 1),
            # previous report: ASELS lighter, EREGL since sold out entirely
            ("AAA", "2026-07-10", "hisse", "BIMAS", "TRE1", "BIM", 90.0, 50.0, 2),
            ("AAA", "2026-07-10", "hisse", "ASELS", "TRE2", "ASELSAN", 40.0, 20.0, 2),
            ("AAA", "2026-07-10", "hisse", "EREGL", "TRE3", "EREGLI", 30.0, 15.0, 2),
        ])
    conn.commit()

hh = c.get("/api/funds/AAA/holdings").json()
assert hh["current_report"] == "2026-08-10" and hh["previous_report"] == "2026-07-10"
sec = hh["sections"]["hisse"]
assert sec["extracted_pct"] == 92.5 and sec["tefas_pct"] == 92.5
assert sec["match"] == "tam", sec
by_code = {i["code"]: i for i in sec["items"]}
assert by_code["BIMAS"]["delta"] == 0.0 and by_code["BIMAS"]["status"] == "mevcut"
assert by_code["ASELS"]["delta"] == 22.5 and by_code["ASELS"]["prev_weight_pct"] == 20.0
# a position that vanished must still be listed, not silently dropped
assert by_code["EREGL"]["status"] == "cikti" and by_code["EREGL"]["weight_pct"] == 0.0
assert by_code["EREGL"]["delta"] == -15.0

# a section the extraction disagrees with must not be offered as detail
assert main._match(92.5, 92.5) == "tam"
assert main._match(92.5, 60.0) == "kismi"
assert main._match(0.0, 40.0) == "eslesmedi"   # TEFAS says none, PDF says plenty
assert main._match(50.0, 0.0) == "yok"
# The real KAC case: TEFAS reports 85.86% deposits and 14.14% lease certificates,
# while the PDF's "KİRA SERTİFİKALARI" heading covers 94.53%. Neither section may
# be offered as detail -- the headings do not mean the same thing.
assert main._match(14.14, 94.53) == "eslesmedi"
assert main._match(85.86, 0.0) == "yok"

# --- parser, on the real report layouts (no network) ---
from fonlu import holdings as H

TAKASBANK = """HİSSE SENETLERİ
Hisse Türk
OZYSR TL ÖZYAŞAR TREOZYS00025 4.960.212,00 12,021907 29/07/26 80100511 12,310000 61.060.209,72 57,64 46,68 50,50
TEL VE
OZYSR TL ÖZYAŞAR TREOZYS00025 -1.572.212,00 12,021907 31/07/26 80100511 12,310000 -19.353.929,72 -18,27 -14,80 -16,01
GRUP TOPLAMI 175.000,00 70.875.000,00 100,00 0,16 0,16
KİRA SERTİFİKALARI
TRD270127T13 AU1 HAZİNE 27/01/27 177 0,00 2 169.150,00 731,26 28/01/26 0,00 6.198,68 1.048.506.784,64 3,96 2,43 2,43
"""
SIMPLE = """A) HİSSE SENETLERİ
AGESA AGESA HAYAT VE EMEKLİLİK A.Ş 121.320,00 29.468.628,00 3,31%
AKBNK AKBANK T.A.S. 273.937,00 17.367.605,80 1,95%
B) VARANTLAR
XXXXX BAŞKA BİR ŞEY 1,00 2,00 99,00%
"""
# rows from the trade/dividend tables that follow the portfolio table
TRAILING = """HİSSE SENETLERİ
AKFYE TL AKFEN 375.600,00 25,453390 31/07/26 80100511 23,880000 8.969.328,00 5,07 4,97 4,65
VI-A-GEÇEN AY İÇİNDE TEMETTÜ
OZSUB Temmettü 08/07/26 357.061,06 357.061,06
"""


class FakePdf:
    """parse_stocks only needs page text; skip a real PDF for the layout tests."""
    def __init__(self, text): self.pages = [type("P", (), {"extract_text": lambda s: text})()]
    def __enter__(self): return self
    def __exit__(self, *a): return False


def parse(text, monkey=H.pdfplumber):
    orig = monkey.open
    monkey.open = lambda _: FakePdf(text)
    try:
        return H.parse(b"x")
    finally:
        monkey.open = orig


t = parse(TAKASBANK)
equities = [x for x in t if x["section"] == "hisse"]
assert len(equities) == 1, t                # two lots of one code collapse to one row
assert equities[0]["code"] == "OZYSR" and equities[0]["isin"] == "TREOZYS00025"
assert equities[0]["weight_pct"] == 31.88   # 46,68 + (-14,80), the fund-level column
assert equities[0]["value"] == 41706280.0
assert "ÖZYAŞAR" in equities[0]["issuer"]
# the lease-certificate row belongs to its own section, not to equities
lease = [x for x in t if x["section"] == "kira"]
assert len(lease) == 1 and lease[0]["code"] == "TRD270127T13", lease

s = parse(SIMPLE)
equities = [x for x in s if x["section"] == "hisse"]
assert [x["code"] for x in equities] == ["AGESA", "AKBNK"], s
assert equities[0]["weight_pct"] == 3.31 and equities[0]["value"] == 29468628.0
# "B) VARANTLAR" opens its own section; that row must not land among the equities
assert [x["code"] for x in s if x["section"] == "varant"] == ["XXXXX"], s

tr = parse(TRAILING)
assert [x["code"] for x in tr] == ["AKFYE"], tr  # dividend row must not become a holding
assert tr[0]["weight_pct"] == 4.97

# An unrecognised lettered heading must CLOSE the open section, otherwise its rows
# are silently filed under the previous one. Real case: SUB's investment fund sat
# under "N) KATILMA BELGELERİ" and was being reported as precious metal.
UNKNOWN_HEADING = """L) ALTIN VE KIYMETLİ MADENLER
ALTIN TL HAZİNE TRXDRP012213 26.000,00 70,08 29/07/26 80100481 68,04 1.769.040,00 50,98 4,37 5,04
N) KATILMA BELGELERİ
SUA ÜNLÜ PORTFÖY YÖNETİMİ A.Ş. 18.691.782,00 10.774.765,58 10,18%
Z) BİLİNMEYEN BİR BÖLÜM
FOO BİR ŞEY 1,00 2,00 7,00%
"""
u = parse(UNKNOWN_HEADING)
by_section = {x["code"]: x["section"] for x in u}
assert by_section["SUA"] == "fon", u          # not "maden"
assert by_section["ALTIN"] == "maden", u
assert "FOO" not in by_section, u             # unknown heading closes the section

# ...but "A.Ş." on a continuation line is not a heading; treating it as one cut
# equity lists in half (DNK dropped from 83.6% to 7.8%).
CONTINUATION = """HİSSE SENETLERİ
OZYSR TL ÖZYAŞAR TREOZYS00025 4.960.212,00 12,02 29/07/26 80100511 12,31 61.060.209,72 57,64 46,68 50,50
TEL VE
A.Ş.
DMSAS TL DEMİSAŞ TRADMSAS91E9 3.000.000,00 8,76 28/07/26 80100517 8,18 24.540.000,00 23,17 18,77 20,29
"""
cont = parse(CONTINUATION)
assert [x["code"] for x in cont] == ["OZYSR", "DMSAS"], cont

# Some layouts carry percentage columns mid-row (interest rate, discount) as well
# as the trailing weight columns. Anchoring on the FIRST "%" found no value and
# dropped every row -- two founders' funds extracted 0% until this was fixed.
MID_ROW_PERCENT = """A.PAY
ENJSA.E ENERJİSA ENERJİ AŞ TREENSA00014 0.00% 0 15,901.00 75.35 26.08.2025 0.00% 0 0 108.9 1,731,618.90 2.43% 2.36%
"""
# Sub-headings decide the asset type: funds sit under "DİĞER" with a "Y.Fonu Türk"
# line above them. Missing that put them in the wrong section (or under equities).
SUBHEADINGS = """HİSSE SENETLERİ
Hisse Türk
OZYSR TL ÖZYAŞAR TREOZYS00025 4.960.212,00 12,02 29/07/26 80100511 12,31 61.060.209,72 57,64 46,68 50,50
GRUP TOPLAMI 8.336.492,00 105.931.524,80 100,00 81,01 87,60
DİĞER
Borsa Y.Fonu Türk
GLDTR TL TRYFNBK00055 46.000,00 504,96 03/07/26 80100103 517,50 23.805.000,00 22,28 3,65 3,84
Y.Fonu Türk
SPP - SPARTA TL SPARTA TRYSPRT00027 9.300.660,00 1,54 31/07/26 1,56 14.520.329,90 58,50 11,11 12,01
"""
sub = {x["code"]: x["section"] for x in parse(SUBHEADINGS)}
assert sub == {"OZYSR": "hisse", "GLDTR": "byf", "SPP": "fon"}, sub

# Some layouts list funds inside the equity block with no sub-heading at all.
# A TRY ISIN (or a "PORTFÖY" issuer) marks a fund; TRA/TRE mark real equities.
MIXED = """HİSSE SENETLERİ
ASELS TL ASELSAN TRAASELS91H2 40.000,00 359,94 16/07/26 80100511 342,25 13.690.000,00 13,33 3,58 3,59
HNC TL HEDEF PORTFÖY ÜÇÜNCÜ TRYHDFP00946 1,00 2,00 3,00 4,00 5.000,00 1,00 0,26 0,30
GMSTR QNB FİNANS PORTFÖY GÜMÜŞ BORSA YATIRIM FONU 122.800,00 65.452.400,00 2,16%
"""
mixed = {x["code"]: x["section"] for x in parse(MIXED)}
assert mixed["ASELS"] == "hisse", mixed          # TRA -> gerçek hisse
assert mixed["HNC"] == "fon", mixed              # TRY ISIN -> fon
assert mixed["GMSTR"] == "byf", mixed            # ünvanda "BORSA YATIRIM" -> BYF

# PBR's layout, which broke three separate ways at once:
#  1. fund codes are glued to the name ("PA2-PUSULA"), with no space to split on;
#  2. the fund's OWN name wraps onto lines reading "HİSSE SENEDİ YÖNETİMİ" and
#     "(HİSSE SENEDİ", which a substring match mistook for an equity heading and
#     flipped the section back, splitting the funds across two sections;
#  3. the page header repeats on every page and was glued onto the previous
#     holding's issuer, injecting "PORTFÖY" and making an equity look like a fund.
PBR_LAYOUT = """HİSSE SENETLERİ
Hisse Türk
GUNDG TL GÜNDOĞD 1.700.606,00 816,24 29/07/26 80100517 1.727,00 2.936.946.562,00 12,03 9,49 9,95
U GIDA
PBR-PUSULA PORTFÖY BİRİNCİ DEĞİŞKEN FON
DİĞER
Y.Fonu Türk
PA2-PUSULA TL PUSULA 24.966.420,00 1,00 29/07/26 1,02 25.545.715,84 0,47 0,08 0,09
PORTFÖY ALTIN PORTFÖY TRYPSLP00192
PBR-PUSULA PORTFÖY BİRİNCİ DEĞİŞKEN FON
PCS-PUSULA TL PUSULA 353.357.530,00 6,58 30/07/26 7,50 2.652.148.616,37 48,76 8,57 8,98
PORTFÖY ÜÇÜNCÜ PORTFÖY
HİSSE SENEDİ YÖNETİMİ
SERBEST FON A.Ş.
(HİSSE SENEDİ
YOĞUN FON)
PKZ-PUSULA TL PUSULA 220.153.922,00 9,73 07/07/26 11,61 2.556.793.017,93 47,01 8,26 8,66
PORTFÖY KUZEY PORTFÖY
HİSSE SENEDİ YÖNETİMİ
"""
pbr = parse(PBR_LAYOUT)
sect = {x["code"]: x["section"] for x in pbr}
assert sect == {"GUNDG": "hisse", "PA2": "fon", "PCS": "fon", "PKZ": "fon"}, pbr
# the repeated page header must not end up in the issuer, or "PORTFÖY" in it
# reclassifies a genuine equity as a fund
assert "PUSULA" not in [x for x in pbr if x["code"] == "GUNDG"][0]["issuer"], pbr
assert round(sum(x["weight_pct"] for x in pbr if x["section"] == "fon"), 2) == 16.91

mid = parse(MID_ROW_PERCENT)
assert len(mid) == 1, mid
assert mid[0]["code"] == "ENJSA" and mid[0]["section"] == "hisse"
assert mid[0]["weight_pct"] == 2.36, mid[0]      # trailing column, not the mid-row 0.00%
assert mid[0]["value"] == 1731618.90, mid[0]

assert H._to_float("61.060.209,72") == 61060209.72   # TR
assert H._to_float("122,800.00") == 122800.0         # US
assert H._to_float("2.16%") == 2.16
# weight column (2nd from the end) over 100% means the row came from another table
assert parse("HİSSE SENETLERİ\nAAAA TL X 1,00 2,00 3,00 4,00 900,00 6,00\n") == []

# Eski sqlite holdings semasinin migration testi kalkti: store._migrate() ile
# birlikte sqlite katmani tamamen gitti, test edecek bir sey kalmadi.

print("ok")
