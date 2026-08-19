"""KAP 'Yatirimci Bilgi Formu' PDF'inden ucret/vergi/valor cikarimi.

TEFAS bu alanlari vermiyor; stopaj orani, yillik yonetim ucreti ve alim valoru
yalnizca fonun aylik yayimlanan Yatirimci Bilgi Formu'nda (KIID) yaziyor.

Format notlari (11 gercek formdan):
  - Form iki sutunlu. Sayfayi duz okumak iki sutunu satir satir ic ice
    geciriyor ve cumleler paramparca oluyordu ('takip eden ilk' bir sutunda,
    'hesaplamada' digerinde). Sayfa ortadan ikiye kirpilip once sol sonra sag
    sutun okununca metin duzgun akiyor.
  - Stopaj her formda ayni cumlede: 'Gercek kisilerin ... kazanci %X; tuzel
    kisilerin ...'. Hisse senedi yogun fonlarda %0 cikmasi dogru.
  - Yonetim ucreti bir tablo satiri; etiketle sayi arasina dagitim payi
    parantezleri girebiliyor ('Kurucu (Asgari %35, azami %65) 2,25').
  - Sayi bicimi degisiyor: '2,68' ve '2.68' ikisi de goruluyor.
  - Alim valoru ilk 'takip eden ...' cumlesi: 'takip eden ilk hesaplamada'
    = ertesi gun. Ikinci gecis iade (satim) odemesini anlatir, o baska sey.

ponytail: metin parseri, gordugumuz duzenlere gore ayarli. Eslesmeyen alan
None donuyor -- yanlis bir vergi orani gostermektense bos birakmak dogru.
"""

import functools
import io
import re

import pdfplumber

from .holdings import _to_float, fetch_pdf
from .kap import KapError

SUBJECT = "Yatırımcı Bilgi Formu"

STOPAJ = re.compile(
    r"[Gg]er[çc]ek ki[şs]iler[a-zçğıöşüA-ZÇĞİÖŞÜ ]{0,60}kazanc[ıi]"
    r"[^%\d]{0,20}%\s*(\d{1,2}(?:[.,]\d{1,2})?)")
FEE_LABEL = re.compile(r"[Yy][öo]netim [ÜüUu]creti")
# Ondalikli sayi; onunde baska bir rakam/ayirici olmayacak ki '65) 2,25'
# icindeki 65 ile 2,25 birbirine karismasin.
# Sondaki lookahead surum numarasini eliyor: '7.1.2 inci maddesi' icindeki
# '7.1' ucret sanilmasin.
DECIMAL = re.compile(r"(?<![\d,.])(\d{1,2}[.,]\d{1,3})(?![\d,.]*\d)")
VALOR = re.compile(
    r"takip eden\s+(\d+|ilk|birinci|ikinci|üçüncü|dördüncü|beşinci)\s*\.?\s*"
    r"(?:hesaplama|i[şs]\s?g[üu]n|i[şs]lem g[üu]n)", re.I)
ORDINALS = {"ilk": 1, "birinci": 1, "ikinci": 2, "üçüncü": 3, "dördüncü": 4, "beşinci": 5}
# Yonetim ucreti etiketiyle sayisi arasindaki azami mesafe: tablo hucresi
# arada dagitim paylarini tasiyabiliyor ama bir sonraki satira gecmemeli.
FEE_WINDOW = 140


def text(pdf_bytes: bytes) -> str:
    """Formun metni, sutun sutun okunmus ve tek satira indirilmis."""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            parts = []
            for page in pdf.pages:
                w, h = page.width, page.height
                parts.append(page.crop((0, 0, w / 2, h)).extract_text() or "")
                parts.append(page.crop((w / 2, 0, w, h)).extract_text() or "")
    except Exception as e:  # bozuk/sifreli dosyada pdfplumber cesitli hatalar atar
        raise KapError(f"Bilgi formu okunamadi: {e}") from e
    return re.sub(r"\s+", " ", " ".join(parts))


def _fee(t: str):
    """Etiketten sonraki ilk makul ondalikli sayi. Etiket formda birden fazla
    gecebiliyor ('...Yonetim Ucretini iceren 7.1.2 inci maddesi...'), o yuzden
    sayi bulunana kadar tum gecisler taraniyor."""
    for label in FEE_LABEL.finditer(t):
        for n in DECIMAL.finditer(t[label.end():label.end() + FEE_WINDOW]):
            v = _to_float(n.group(1))
            if v and 0 < v <= 10:   # yillik yonetim ucreti hicbir fonda %10'u gecmiyor
                return v
    return None


def fields(t: str) -> dict:
    """Tek satira indirilmis form metninden alanlar."""
    stopaj, valor = STOPAJ.search(t), VALOR.search(t)
    return {
        "stopaj_pct": _to_float(stopaj.group(1)) if stopaj else None,
        "management_fee_pct": _fee(t),
        "valor_days": ORDINALS.get(valor.group(1).lower(), _to_float(valor.group(1)))
                      if valor else None,
    }


def parse(pdf_bytes: bytes) -> dict:
    return fields(text(pdf_bytes))


@functools.lru_cache(maxsize=256)
def fetch(disclosure_index: int) -> dict:
    """Bildirimin ekini indirip ayristirir.

    ponytail: surec ici cache yeter, ucretler yilda bir degisiyor; yeniden
    baslayinca ilk acilista tekrar indiriliyor. Tabloya yazmak gerekirse
    holdings'teki desen hazir.
    """
    from . import kap  # dairesel import olmasin diye burada

    atts = kap.attachments(disclosure_index)
    if not atts:
        raise KapError("Bilgi formunun eki yok.")
    return parse(fetch_pdf(atts[0]["obj_id"]))
