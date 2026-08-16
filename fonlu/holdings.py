"""KAP aylik 'Portfoy Dagilim Raporu' PDF'inden kalem bazli portfoy cikarimi.

TEFAS sadece varlik sinifi yuzdesi veriyor (stock_pct, investment_fund_pct...);
fonun o sinif icinde HANGI kalemleri tuttugu yalnizca bu PDF'in portfoy degeri
tablosunda var. Tablo varlik sinifi basliklariyla bolumlere ayrilmis
(HISSE SENETLERI, KIRA SERTIFIKALARI, YATIRIM FONU...); her kalem kendi
bolumuyle etiketlenip donuyor, boylece TEFAS dagilim satirina baglanabiliyor.

Gercek raporlardan cikan format notlari:
  - KAP'in dosya endpoint'i PDF'i Java-serialize edilmis bir bayt dizisi icinde
    donduruyor; gercek icerik '%PDF-' ofsetinden sonra basliyor.
  - Iki satir ailesi var:
      A) Takasbank duzeni: KOD PARA_BIRIMI IHRACCI [ISIN] NOMINAL FIYAT TARIH
         ... DEGER %grup %fpd %ftd   -> agirlik sondan ikinci ciplak sayi
      B) Sade duzen:       KOD IHRACCI NOMINAL DEGER %agirlik
         -> agirlik '%' isaretli son token
  - Sayi bicimi degisiyor: '1.234,56' (TR) ve '1,234.56' (US). En sagdaki
    ayirici ondalik kabul ediliyor.
  - Hisse satiri BIST koduyla, tahvil/kira sertifikasi satiri ISIN ile basliyor;
    kalemin kimligi buna gore seciliyor.
  - Ana bolum basliginin altinda ALT BASLIK olabiliyor ve varlik turunu asil o
    belirliyor: 'DİĞER' bolumunun altindaki 'Y.Fonu Türk' satirindan sonrasi
    yatirim fonu, 'Borsa Y.Fonu Türk' sonrasi BYF. Alt basliklar taninmazsa
    fonlar bir ustteki bolumde (DİĞER, hatta hisse) gorunuyor.
  - Ihracci unvani alt satirlara tasabiliyor, sayfa basliklari tekrar ediyor.
  - Ayni kalem birden cok satirda (farkli lotlar, negatif satis satirlari)
    gorunur; kod bazinda toplanmalari gerekir.

ponytail: metin bazli parser, kurucuya gore duzen degisiyor. Cikarim basarisiz
olursa bos donuyor -- hatali veri gostermektense hic gostermemek dogru.
"""

import io
import re
from collections import Counter

import pdfplumber
import requests

from .kap import ATTACHMENT_URL, HEADERS, KapError

# PDF bolum basligi -> bolum anahtari. Sirali: ilk eslesen kazanir, bu yuzden
# ozel olanlar ('BORSA YATIRIM') genel olanlardan ('YATIRIM FONU') once.
# Baslik satiri TAM eslesmeli. 'Icinde geciyor mu' aramasi, fonun kendi adinin
# alt satira tasan parcasini baslik sanip bolumu degistiriyordu: PBR raporunda
# 'PUSULA PORTFÖY ... HİSSE SENEDİ FONU (HİSSE SENEDİ YOĞUN FON)' adinin
# devami olan 'HİSSE SENEDİ YÖNETİMİ' ve '(HİSSE SENEDİ' satirlari bolumu
# yatirim fonundan hisseye dondurup fonlari ikiye boluyordu.
SECTIONS = [
    ("hisse", ("HİSSE SENETLERİ", "HİSSE SENEDİ", "HİSSE", "A.PAY", "A. PAY",
               "ORTAKLIK PAYLARI", "PAY SENETLERİ")),
    ("byf", ("BORSA YATIRIM FONLARI", "BORSA YATIRIM FONU", "BORSA Y.FONU",
             "BORSA Y. FONU", "BYF")),
    # 'KATILMA BELGELERİ' fon katilma belgesi demek; 'KATILMA HESAPLARI' mevduat.
    ("fon", ("YATIRIM FONU", "YATIRIM FONLARI", "Y.FONU", "Y. FONU",
             "KATILMA BELGELERİ", "KATILMA PAYLARI", "FON KATILMA BELGELERİ")),
    ("kira", ("KİRA SERTİFİKALARI", "KİRA SERTİFİKASI", "KAMU KESİMİ KİRA SERTİFİKALARI",
              "ÖZEL SEKTÖR KİRA SERTİFİKALARI", "KAMU KİRA SERTİFİKALARI")),
    ("borclanma", ("BORÇLANMA SENETLERİ", "BORÇLANMA ARAÇLARI", "DEVLET TAHVİLİ",
                   "DEVLET TAHVİLİ VE BONOLAR", "ÖZEL SEKTÖR TAHVİLLERİ",
                   "ÖZEL SEKTÖR TAHVİLİ", "HAZİNE BONOSU", "FİNANSMAN BONOLARI",
                   "FİNANSMAN BONOSU", "BANKA BONOLARI", "BANKA BONOSU",
                   "TAHVİL VE BONO", "VARLIĞA DAYALI MENKUL KIYMETLER",
                   "GELİR ORTAKLIĞI SENETLERİ", "GELİRE ENDEKSLİ SENETLER",
                   "DÖVİZE ENDEKSLİ TAHVİLLER", "KAMU BORÇLANMA ARAÇLARI",
                   "ÖZEL SEKTÖR BORÇLANMA ARAÇLARI")),
    ("eurobond", ("EUROBOND", "EUROBONDLAR")),
    ("repo", ("REPO", "TERS REPO", "REPO / TERS REPO", "REPO/TERS REPO")),
    ("parapiyasasi", ("PARA PİYASASI", "TAKASBANK PARA PİYASASI",
                      "BİST PARA PİYASASI", "PARA PİYASASI İŞLEMLERİ")),
    ("mevduat", ("MEVDUAT", "KATILMA HESAPLARI", "KATILMA HESABI", "VADELİ MEVDUAT",
                 "VADESİZ MEVDUAT")),
    ("maden", ("ALTIN VE KIYMETLİ MADENLER", "KIYMETLİ MADENLER", "DEĞERLİ MADENLER",
               "ALTIN", "KIYMETLİ MADEN", "DEĞERLİ MADEN")),
    ("turev", ("TÜREV ARAÇLAR", "TÜREV ARAÇLARI", "VİOP İŞLEMLERİ", "VIOP İŞLEMLERİ",
               "VİOP NAKİT TEMİNAT İŞLEMLERİ", "VIOP NAKİT TEMİNATI",
               "VİOP NAKİT TEMİNATI", "OPSİYON", "VADELİ İŞLEMLER", "FUTURES")),
    ("varant", ("VARANTLAR", "VARANT")),
    ("yabanci", ("YABANCI SABİT GETİRİLİ MENKUL KIYMETLER", "YABANCI MENKUL KIYMETLER",
                 "YABANCI HİSSE SENETLERİ", "YABANCI BORÇLANMA ARAÇLARI")),
    ("diger", ("DİĞER",)),
]
# Baslik satirinin sonundaki nitelik kelimeleri ('Hisse Türk', 'Y.Fonu Yabancı').
HEADING_QUALIFIER = re.compile(
    r"\s+(TÜRK|TURK|YABANCI|YAB\.?|TL|DÖVİZ|DOVIZ|USD|EUR)\.?$")
# 'A)' 'N)' '1-' gibi numarali baslik satiri. Tanimadigimiz boyle bir baslik
# gorulunce acik bolum KAPATILIYOR: aksi halde altindaki kalemler bir onceki
# bolume yaziliyordu (SUB'da 'N) KATILMA BELGELERİ' altindaki fon, taninmadigi
# icin bir ustteki 'L) ALTIN VE KIYMETLİ MADENLER' bolumune dusuyordu).
# Parantez sart: 'A.Ş.' gibi ihracci devam satirlari da nokta/tire ile eslesip
# bolumu erken kapatiyordu ve hisse listeleri ortasindan kesiliyordu.
ENUM_HEADING = re.compile(r"^([A-ZÇĞİÖŞÜ]{1,2}|\d{1,2})\s*\)\s+[A-ZÇĞİÖŞÜ]{2}")
ENUM_PREFIX = re.compile(r"^([A-ZÇĞİÖŞÜ]{1,2}|\d{1,2})\s*[\)\.\-]\s+")
# Portfoy tablosundan sonraki islem tablolari da kod + sayi iceriyor ama tasinan
# pozisyon degil (temettu tahsilati, alim satim, bedelsiz pay).
TABLE_END = ("TEMETTÜ", "TEMETTU", "GEÇEN AY", "ALIM SATIM", "ALIM-SATIM", "BEDELSİZ",
             "RÜÇHAN", "TOPLAM DEĞERİ TABLOSU", "AY İÇİNDE YAPILAN")
ROMAN_SECTION = re.compile(r"^[IVX]{1,4}\s*-")

NOT_CODE = {"GRUP", "ARA", "TOPLAM", "TOPLAMI", "HISSE", "HİSSE", "SAYFA", "FON",
            "TL", "USD", "EUR", "GENEL", "NOT", "TCMB", "VADEYE", "DÖVİZ", "MENKUL",
            "VIOP", "BIST", "BİST", "SPK", "KAP", "AU1", "XAU"}
TOKEN = re.compile(r"-?\d[\d.,]*\d|-?\d")
# Kod ile devami arasinda bosluk olmayabiliyor: fon satirlari 'PA2-PUSULA PORTFÖY
# ALTIN...' seklinde geliyor ve sadece bosluk kabul etmek bu satirlari komple
# dusuruyordu (PBR'de 5 fondan 4'u kayboluyordu).
ROW = re.compile(r"^(?P<code>[A-Z][A-ZÇĞİÖŞÜ0-9]{2,5})(?:\.E)?(?:\s*-\s*|\s+)(?P<rest>.+)$")
ISIN = re.compile(r"\b(TR[A-Z0-9]{10}|US[A-Z0-9]{10})\b")
CCY_PREFIX = re.compile(r"^(TL|USD|EUR|GBP|XAU|AU\d?)\s+", re.I)

# TEFAS dagilim sutunu -> PDF bolumu. Arayuz bir dagilim satirini ancak burada
# karsiligi varsa tiklanabilir yapiyor.
TEFAS_TO_SECTION = {
    "stock_pct": "hisse", "foreign_stock_pct": "hisse",
    "investment_fund_pct": "fon", "fund_participation_certificate_pct": "fon",
    # Girisim sermayesi / gayrimenkul yatirim fonu da fondur; eslenmeyince
    # cikarilan agirlik TEFAS'takinden fazla gorunup bolum gizleniyordu.
    "venture_capital_fund_pct": "fon", "real_estate_fund_pct": "fon",
    "etf_pct": "byf", "foreign_etf_pct": "byf", "precious_metals_etf_pct": "byf",
    "government_lease_certificate_pct": "kira",
    "government_lease_certificate_tl_pct": "kira",
    "government_lease_certificate_fx_pct": "kira",
    "private_sector_lease_certificate_pct": "kira",
    "government_bond_pct": "borclanma", "treasury_bill_pct": "borclanma",
    "private_sector_bond_pct": "borclanma", "financing_bill_pct": "borclanma",
    "bank_bill_pct": "borclanma", "asset_backed_securities_pct": "borclanma",
    "eurobond_pct": "eurobond",
    "repo_pct": "repo", "reverse_repo_pct": "repo",
    "takasbank_money_market_pct": "parapiyasasi", "bist_money_market_pct": "parapiyasasi",
    "term_deposit_pct": "mevduat", "deposit_tl_pct": "mevduat",
    "deposit_fx_pct": "mevduat", "deposit_gold_pct": "mevduat",
    "participation_account_pct": "mevduat", "participation_account_tl_pct": "mevduat",
    "precious_metals_pct": "maden", "precious_metals_government_debt_pct": "maden",
    "derivative_pct": "turev", "futures_cash_collateral_pct": "turev",
    "foreign_security_pct": "yabanci", "foreign_debt_security_pct": "yabanci",
    "other_pct": "diger",
}
SECTION_LABELS = {
    "hisse": "Hisse senedi", "byf": "Borsa yatırım fonu", "fon": "Yatırım fonu",
    "kira": "Kira sertifikası", "borclanma": "Borçlanma aracı", "eurobond": "Eurobond",
    "repo": "Repo", "parapiyasasi": "Para piyasası", "mevduat": "Mevduat",
    "maden": "Kıymetli maden",
    "turev": "Türev araç", "varant": "Varant", "yabanci": "Yabancı menkul kıymet",
    "diger": "Diğer",
}


def _heading(upper_line: str, phrases) -> bool:
    """Bolum basligi mi? Basliklar 'A) HISSE SENETLERI' gibi numaralanmis
    olabiliyor, ama ayni ifade duz cumlenin ortasinda da geciyor ('...BEDELSIZ
    HISSE SENEDI ALIMI...'). Satir basina yakinlik ikisini ayiriyor."""
    return any(0 <= upper_line.find(p) <= 8 for p in phrases)


def _section_of(upper_line: str):
    """Satir bir bolum basligiysa bolum anahtarini dondurur.

    Tam eslesme sarti: bir kalemin unvanindan tasan parca ('HİSSE SENEDİ
    YÖNETİMİ', '(HİSSE SENEDİ') baslik sayilmamali.
    """
    text = HEADING_QUALIFIER.sub("", upper_line.strip(" :.-")).strip()
    for key, phrases in SECTIONS:
        if text in phrases:
            return key
    return None


def _reclassify(item: dict) -> dict:
    """Hisse bolumunde listelenmis fon/BYF kalemlerini dogru bolume tasir.

    Bazi raporlar fon ve BYF kalemlerini ayri bir alt baslik acmadan hisse
    blogunun icine koyuyor (HMC'de Hedef Portfoy fonlari, OTJ'de GMSTR/ZGOLD).
    Iki yapisal isaret var:
      - Turk fon ISIN'leri 'TRY' ile, hisseler 'TRA'/'TRE' ile basliyor;
        'TRD' kira sertifikasi.
      - ISIN yakalanamadiysa unvanda 'PORTFÖY' geciyor -- BIST'te islem goren
        bir sirketin unvani boyle bitmez, portfoy sirketinin fonudur.
    """
    if item["section"] != "hisse":
        return item
    isin, issuer = item["isin"] or "", (item["issuer"] or "").upper()
    is_fund = isin.startswith("TRY") or (not isin and "PORTFÖY" in issuer)
    if isin.startswith("TRD"):
        item["section"] = "kira"
    elif is_fund:
        item["section"] = "byf" if "BORSA YATIRIM" in issuer or "BOR" == issuer[-3:] else "fon"
    return item


def _clean_issuer(s: str) -> str:
    """Unvani toparlar; okunamaz haldeyse bos dondurur.

    Bazi raporlarda unvan hucreye dikey sigdirildigi icin pdfplumber harfleri
    'A E S LE E N K L S' gibi karisik dokuyor. Boyle bir metni gostermektense
    hic gostermemek dogru -- kalemin kimligi zaten kodu."""
    s = CCY_PREFIX.sub("", re.sub(r"\s+", " ", s).strip(" -"))
    s = re.sub(r"\b(TR|US)[A-Z0-9]{0,9}\b$", "", s).strip()
    tokens = s.split()
    if not tokens:
        return ""
    tiny = sum(1 for t in tokens if len(t) <= 2)
    if len(tokens) >= 4 and tiny / len(tokens) > 0.4:
        return ""
    return s


def _to_float(s: str):
    """'61.060.209,72' ve '122,800.00' -> float. En sagdaki ayirici ondalik."""
    s = s.strip().rstrip("%")
    dot, comma = s.rfind("."), s.rfind(",")
    if dot > comma:
        s = s.replace(",", "")
    elif comma > dot:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def fetch_pdf(obj_id: str, timeout=60) -> bytes:
    """KAP ekini indirir ve Java-serialize sarmalayicisindan PDF'i cikarir."""
    try:
        r = requests.get(ATTACHMENT_URL.format(obj_id), headers=HEADERS, timeout=timeout)
        r.raise_for_status()
    except requests.RequestException as e:
        raise KapError(f"KAP eki indirilemedi: {e}") from e
    start = r.content.find(b"%PDF-")
    if start < 0:
        raise KapError("Ek bir PDF degil.")
    return r.content[start:]


def _parse_row(rest: str):
    """(deger, agirlik_yuzdesi) veya None."""
    tokens = [t.group() for t in TOKEN.finditer(rest)]
    if len(tokens) < 2:
        return None
    parts = rest.split()
    # Agirlik satirin SONUNDAKI '%' blogunda. Bazi duzenlerde satirin ortasinda da
    # yuzde sutunlari var (faiz orani gibi); ilk '%' isaretini esas almak degeri
    # bulamayip satiri tamamen dusuruyordu.
    tail = len(parts)
    while tail > 0 and parts[tail - 1].endswith("%"):
        tail -= 1
    if tail < len(parts):
        weight = _to_float(parts[-1])
        before = [p for p in parts[:tail] if TOKEN.fullmatch(p)]
        value = _to_float(before[-1]) if before else None
    elif len(tokens) >= 4:
        value, weight = _to_float(tokens[-4]), _to_float(tokens[-2])
    else:
        return None
    if value is None or weight is None:
        return None
    return value, weight


def parse(pdf_bytes: bytes) -> list[dict]:
    """Tum bolumlerdeki kalemleri (bolum, kod) bazinda toplayip dondurur."""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception as e:  # bozuk/sifreli dosyalarda pdfplumber cesitli hatalar atar
        raise KapError(f"PDF okunamadi: {e}") from e

    # Sayfa basliklari (raporun kendi fon adi, tablo sutun basliklari) her sayfada
    # tekrar ediyor ve unvan devami sanilip kaleme yapisiyordu: GUNDOGDU GIDA'nin
    # unvanina 'PBR-PUSULA PORTFÖY ... FON' eklenince hisse, fon sanilmisti.
    lines = [raw.strip() for raw in text.splitlines()]
    counts = Counter(l for l in lines if l and not TOKEN.search(l))
    # Bolum basliklari da tekrar edebiliyor ('Y.Fonu Türk' bir raporda iki kez);
    # onlari eleme, yoksa bolum hic acilmaz.
    furniture = {l for l, n in counts.items()
                 if n > 1 and _section_of(ENUM_PREFIX.sub("", l.upper())) is None}

    agg, section, last = {}, None, None
    for line in lines:
        if not line or line in furniture:
            continue
        upper = line.upper()

        if _heading(upper, TABLE_END) or ROMAN_SECTION.match(upper):
            section, last = None, None
            continue
        # Baslik satirinda (numaralandirma disinda) rakam olmaz. Bu sart olmadan
        # kodu bir bolum kelimesiyle baslayan VERI satiri -- ornegin 'ALTIN TL
        # HAZINE ... 1.769.040,00' -- baslik sanilip kalem olarak kaybediliyor.
        body = ENUM_PREFIX.sub("", upper)
        if not TOKEN.search(body):
            if (found_section := _section_of(body)) is not None:
                section, last = found_section, None
                continue
            # Taninmayan numarali baslik: bolumu kapat, kalemleri onceki bolume yazma.
            if ENUM_HEADING.match(upper):
                section, last = None, None
                continue
        if section is None:
            continue

        # Tahvil/kira satiri ISIN ile, hisse satiri BIST koduyla basliyor.
        if (lead := ISIN.match(line)):
            code, rest = lead.group(), line[lead.end():].strip()
        else:
            m = ROW.match(line)
            if not m or m.group("code") in NOT_CODE:
                if last:
                    if (found := ISIN.search(line)) and not last["isin"]:
                        last["isin"] = found.group()
                    elif (not TOKEN.search(line) and len(line) < 40
                          and line not in last["issuer"]):
                        last["issuer"] = f"{last['issuer']} {line}".strip()
                continue
            code, rest = m.group("code"), m.group("rest")

        parsed = _parse_row(rest)
        if parsed is None:
            continue
        value, weight = parsed
        # Tek kalem portfoyun %100'unden fazlasi olamaz; boyle bir satir portfoy
        # tablosu disindan gelmistir.
        if abs(weight) > 100:
            continue

        key = (section, code)
        row = agg.get(key)
        if row is None:
            row = agg[key] = {
                "section": section, "code": code,
                "isin": (f.group() if (f := ISIN.search(rest)) else
                         code if code.startswith(("TR", "US")) and len(code) == 12 else ""),
                "issuer": re.split(r"\s(?=-?\d)", ISIN.sub("", rest).strip(), 1)[0].strip(),
                "value": 0.0, "weight_pct": 0.0,
            }
        row["value"] += value
        row["weight_pct"] += weight
        last = row

    out = [
        _reclassify({**r, "issuer": _clean_issuer(r["issuer"]),
                     "value": round(r["value"], 2),
                     "weight_pct": round(r["weight_pct"], 2)})
        for r in agg.values()
        # Tamamen kapatilmis pozisyon (alis + esit satis) portfoyde durmuyor.
        if abs(r["value"]) >= 0.005
    ]
    # Kaldiracli fonda bile toplam ~%125'i gecmiyor. Daha yuksek bir toplam
    # tablonun yanlis sutunundan okundugunu gosterir.
    if sum(r["weight_pct"] for r in out) > 250:
        return []
    out.sort(key=lambda r: -r["weight_pct"])
    return out
