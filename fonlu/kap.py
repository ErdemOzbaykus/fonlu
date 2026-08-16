"""KAP fon bildirimleri (kap.org.tr, auth yok).

POST /tr/api/disclosure/funds/byCriteria bir tarih araligindaki TUM fon
bildirimlerini `fundCode` ile birlikte doner -- bu kod TEFAS fon koduyla ayni,
join icin baska bir esleme gerekmiyor.

Kapsam notu: KAP fonun hisse bazli portfoyunu yapisal veri olarak VERMIYOR.
"Portfoy Dagilim Raporu" bildiriminin govdesi bos bir XBRL formu; gercek detay
PDF ekinin icinde. Bu yuzden burada bildirim listesi + eke dogrudan link
tutuluyor, kirilimi PDF'ten cikarmaya calismiyoruz.
"""

import time
from datetime import date, timedelta

import requests

API = "https://www.kap.org.tr/tr/api/disclosure/funds/byCriteria"
DISCLOSURE_URL = "https://www.kap.org.tr/tr/Bildirim/{}"
ATTACHMENT_URL = "https://www.kap.org.tr/tr/api/file/download/{}"
HEADERS = {
    "Content-Type": "application/json",
    "Accept-Language": "tr",
    "Referer": "https://www.kap.org.tr/tr/bildirim-sorgu",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
}
# The API caps a response at 2000 rows and has no paging parameter, so the range
# is walked in windows. Disclosure volume spikes at month end (portfolio and
# financial reports), so a window that hits the cap is halved and retried.
CHUNK_DAYS = 7
ROW_CAP = 2000


class KapError(RuntimeError):
    pass


def fetch(start: date, end: date, timeout=30) -> list[dict]:
    try:
        r = requests.post(
            API,
            json={"fromDate": start.isoformat(), "toDate": end.isoformat()},
            headers=HEADERS,
            timeout=timeout,
        )
        r.raise_for_status()
        rows = r.json()
    except requests.RequestException as e:
        raise KapError(f"KAP'a ulasilamadi: {e}") from e
    except ValueError as e:
        raise KapError(f"KAP gecersiz yanit dondu: {e}") from e
    if not isinstance(rows, list):
        raise KapError(f"KAP beklenmeyen yanit: {str(rows)[:200]}")
    return rows


def fetch_complete(start: date, end: date) -> list[dict]:
    """fetch(), halving the window whenever the row cap truncates the answer."""
    rows = fetch(start, end)
    if len(rows) < ROW_CAP or start == end:
        return rows
    mid = start + (end - start) // 2
    time.sleep(1)
    left = fetch_complete(start, mid)
    time.sleep(1)
    return left + fetch_complete(mid + timedelta(days=1), end)


def _iso(publish_date: str) -> str:
    """'14.08.2026 23:46:28' -> '2026-08-14 23:46:28' (sortable)."""
    try:
        d, _, t = publish_date.partition(" ")
        dd, mm, yy = d.split(".")
        return f"{yy}-{mm}-{dd} {t}".strip()
    except (ValueError, AttributeError):
        return publish_date or ""


def sync(conn, days=90, log=print):
    """Walk [today-days, today] in chunks and upsert every fund disclosure."""
    end, total = date.today(), 0
    start = end - timedelta(days=days)
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=CHUNK_DAYS - 1), end)
        rows = fetch_complete(cur, chunk_end)
        conn.executemany(
            "INSERT OR REPLACE INTO kap_disclosures VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    r["disclosureIndex"],
                    r["fundCode"],
                    _iso(r["publishDate"]),
                    r.get("kapTitle"),
                    r.get("subject"),
                    r.get("summary"),
                    r.get("disclosureClass"),
                    r.get("attachmentCount") or 0,
                )
                for r in rows
                if r.get("fundCode") and r.get("disclosureIndex")
            ],
        )
        conn.commit()
        total += len(rows)
        log(f"KAP {cur} -> {chunk_end}: {len(rows)} bildirim")
        cur = chunk_end + timedelta(days=1)
        time.sleep(1)  # ponytail: KAP'in limiti belgeli degil, nazik davran
    log(f"KAP bitti. {total} bildirim islendi.")
    return total


def attachments(disclosure_index: int, timeout=30) -> list[dict]:
    """Bir bildirimin eklerini dondurur: [{'file_name', 'url'}]. Detay endpoint'i
    yavas olabildigi icin sadece kullanici bir bildirime tikladiginda cagrilir."""
    url = f"https://www.kap.org.tr/tr/api/notification/attachment-detail/{disclosure_index}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        raise KapError(f"KAP eki alinamadi: {e}") from e
    out = []
    for item in data if isinstance(data, list) else []:
        for a in item.get("attachments") or []:
            if a.get("objId"):
                out.append({
                    "file_name": a.get("fileName") or a["objId"],
                    "obj_id": a["objId"],
                })
    return out
