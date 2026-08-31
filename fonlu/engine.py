"""Fon rotasyon motoru.

Saf fonksiyon: NAV serileri + config -> sepet. DB'ye, HTTP'ye, TEFAS'a
dokunmaz, bu yuzden ayni motor hem canli oneride hem backtest'te kosar.

Pencere gozlem sayisiyla degil TARIHLE kesiliyor: TEFAS yilda ~250 islem gunu
veriyor, "son 252 satir" istemek havuzu bosaltirdi. Ayrica backtest'in gecmis
bir as_of'ta calisabilmesi icin ankorun tarih olmasi sart.
"""

import calendar
import re
from dataclasses import dataclass, field
from datetime import date

# SPK unvanda bu ibareleri zorunlu tutuyor; yani isim tahmin degil resmi etiket.
_OZEL = re.compile(r"\bÖZEL\b", re.I)          # onceden belirli gruba satilir, kimse alamaz
_SERBEST = re.compile(r"\bSERBEST\b", re.I)    # sadece nitelikli yatirimci alabilir


@dataclass(frozen=True)
class Config:
    taban_agirlik: float = 0.30
    slot: int = 2
    pencere_ay: int = 12
    esik_bandi: float = 1.05     # carpimsal
    esik_mutlak: float = 0.15    # sifira yakin skorda carpimsal bant korumuyor
    min_volatilite: float = 0.03
    max_volatilite: float = 0.60
    min_yatirimci: int = 5000
    akis_gun: int = 63           # ~3 ay islem gunu
    akis_kisa_gun: int = 21      # ~1 ay; ivme icin son dilim
    min_akis: float = -0.10      # pencere basi buyuklugun %10'undan fazlasi cikmissin
    # Ivme yalnizca akis NEGATIFKEN baglayici: cikis varsa "yavasliyor ama
    # hizlanmiyor" mazeret degil, giris varsa hafif yavaslama eleme sebebi degil.
    # Tek basina sart kosuldugunda havuz 92 -> 24'e dusuyor ve kanamasi yavaslayan
    # fonlar (akis %-6.7, vol %55) para ceken fonlarin yerine geciyordu.
    min_ivme: float = 0.0
    haric_sinif: tuple = ("Para Piyasası", "Borçlanma Aracı")
    # Ikinci slot birincisiyle bu korelasyonun uzerindeyse alinmaz. Sinif etiketi
    # bunun kotu bir vekili: "Fon Sepeti" bir fonun yarim iletken Hisse fonlariyla
    # korelasyonu 0.92 cikabiliyor, ayni sinif iki fonunki 0.46 kalabiliyor.
    max_korelasyon: float = 0.80
    korelasyon_gun: int = 126    # ~6 ay


@dataclass(frozen=True)
class Fon:
    kod: str
    ad: str
    sinif: str
    yatirimci: int
    seri: list          # [(date, price, pay_sayisi)], tarihe gore artan


@dataclass(frozen=True)
class Aday:
    kod: str
    sinif: str
    getiri: float
    volatilite: float
    fazla: float
    skor: float
    akis: float | None
    ivme: float | None
    # repr=False: 126 gunluk sozluk her assert mesajini okunamaz hale getiriyor
    getiriler: dict = field(repr=False)   # {date: gunluk getiri}, korelasyon icin


def _ay_once(d: date, ay: int) -> date:
    """'12 ay once' = takvim ayi, 365 gun degil. main.py'deki _months_back ile
    ayni kural; motor DB'siz import edilebilsin diye kopyalandi."""
    total = d.year * 12 + (d.month - 1) - ay
    yil, ay_ = divmod(total, 12)
    ay_ += 1
    return date(yil, ay_, min(d.day, calendar.monthrange(yil, ay_)[1]))


def metrik(seri, as_of: date, cfg=Config()):
    """(getiri, yillik volatilite) ya da veri yetmiyorsa None."""
    p = [(x[0], x[1]) for x in seri if x[0] <= as_of and x[1]]
    if not p:
        return None
    hedef = _ay_once(p[-1][0], cfg.pencere_ay)
    onceki = [i for i, (d, _) in enumerate(p) if d <= hedef]
    if not onceki:
        return None                       # 12 ay geriye yetmiyor
    pen = [v for _, v in p[onceki[-1]:]]
    rets = [b / a - 1 for a, b in zip(pen, pen[1:]) if a]
    if len(rets) < 200:                   # ~10 ay islem gunu; delikli seri gecmesin
        return None
    ort = sum(rets) / len(rets)
    var = sum((r - ort) ** 2 for r in rets) / (len(rets) - 1)
    return pen[-1] / pen[0] - 1, var ** 0.5 * (252 ** 0.5)


def _akis(p):
    """Net giris/cikis, pencere BASINDAKI buyuklugun yuzdesi.

    Payda olarak guncel buyuklugu kullanmak yariya inen fonda %-100'u asan
    anlamsiz oranlar veriyor. buyukluk = fiyat x pay sayisi, ayri sutun gerekmiyor.
    """
    if len(p) < 5:
        return None
    bas = p[0][1] * p[0][2]
    return sum((b[2] - a[2]) * b[1] for a, b in zip(p, p[1:])) / bas if bas else None


def nakit_akis(seri, as_of: date, cfg=Config()):
    """(uzun pencere akisi, ivme).

    ivme = son kisa pencerenin gunluk akis hizi eksi ONDAN ONCEKI dilimin hizi.
    Pencereler cakismiyor: kisa pencere uzunun icinde kalirsa ayni para iki kere
    sayilip ivme sonuyor.
    """
    p = [x for x in seri if x[0] <= as_of and x[1] and x[2] is not None]
    if len(p) < cfg.akis_gun // 2:
        return None, None
    k, u = cfg.akis_kisa_gun, cfg.akis_gun
    kisa, onceki = _akis(p[-k:]), _akis(p[-u:-k])
    ivme = None if kisa is None or onceki is None else kisa / k - onceki / (u - k)
    return _akis(p[-u:]), ivme


def adaylar(fonlar, taban: Fon, as_of: date, nitelikli: bool, cfg=Config()):
    """Filtreleri gecen fonlar, skora gore azalan."""
    rf = metrik(taban.seri, as_of, cfg)
    if rf is None:
        raise ValueError(f"Taban fon {taban.kod} icin {cfg.pencere_ay} aylik veri yok")
    rf = rf[0]
    out = []
    for f in fonlar:
        if _OZEL.search(f.ad):
            continue
        if _SERBEST.search(f.ad) and not nitelikli:
            continue
        if f.sinif in cfg.haric_sinif or f.yatirimci < cfg.min_yatirimci:
            continue
        m = metrik(f.seri, as_of, cfg)
        if m is None:
            continue
        g, v = m
        if not cfg.min_volatilite <= v <= cfg.max_volatilite:
            continue
        fazla = g - rf
        if fazla <= 0:                    # ayi piyasasinda havuz bosalir; tasarim boyle
            continue
        akis, ivme = nakit_akis(f.seri, as_of, cfg)
        # 12 aylik momentum eski kazanctan geliyor olabilir; para cikiyorsa alma.
        # Veri yoksa cezalandirmiyoruz (pay sayisi TEFAS'ta 2486/2486 fonda dolu).
        if akis is not None and akis < cfg.min_akis:
            continue
        # Cikan fonda ivme de dusukse alma; giren fonda ivmeye bakmiyoruz.
        if akis is not None and akis < 0 and ivme is not None and ivme < cfg.min_ivme:
            continue
        p = [(x[0], x[1]) for x in f.seri if x[0] <= as_of and x[1]][-cfg.korelasyon_gun - 1:]
        gunluk = {b[0]: b[1] / a[1] - 1 for a, b in zip(p, p[1:]) if a[1]}
        out.append(Aday(f.kod, f.sinif, g, v, fazla, fazla / v, akis, ivme, gunluk))
    out.sort(key=lambda a: -a.skor)
    return out


def korelasyon(a, b, cfg=Config()):
    """Iki adayin gunluk getirileri arasindaki Pearson korelasyonu.

    Ortak islem gunleri uzerinden: fonlarin fiyat acikladigi gunler birebir
    ayni degil, hizalamadan hesaplamak korelasyonu sulandiriyor.
    """
    ortak = sorted(a.getiriler.keys() & b.getiriler.keys())[-cfg.korelasyon_gun:]
    if len(ortak) < cfg.korelasyon_gun // 2:
        return None
    x = [a.getiriler[d] for d in ortak]
    y = [b.getiriler[d] for d in ortak]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    pay = sum((i - mx) * (j - my) for i, j in zip(x, y))
    payda = (sum((i - mx) ** 2 for i in x) * sum((j - my) ** 2 for j in y)) ** 0.5
    return pay / payda if payda else None


def _cakisir(a, secilen, cfg):
    for s in secilen:
        if s.kod == a.kod:
            return True
        k = korelasyon(a, s, cfg)
        # Olculemiyorsa sinif etiketine dusuyoruz: vekil kotu ama hicten iyi.
        if (k is None and s.sinif == a.sinif) or (k is not None and k > cfg.max_korelasyon):
            return True
    return False


def sepet(adaylar_, mevcut=(), cfg=Config()):
    """(secilen adaylar, para piyasasi agirligi).

    Eldeki fon ancak bekleyen aday esik bandini asarsa cikarilir. Fazla getirisi
    negatife donen fon zaten `adaylar_` icinde olmaz -> spec'teki "esik aranmaz"
    istisnasi kendiliginden isliyor.
    """
    kalan = list(adaylar_)
    secilen = []
    for kod in mevcut:
        if len(secilen) >= cfg.slot:
            break
        a = next((x for x in kalan if x.kod == kod), None)
        if a is None:
            continue                      # havuzdan dustu -> bant aranmadan cikar
        rakip = next((x for x in kalan if x.kod != kod and not _cakisir(x, secilen, cfg)), None)
        if rakip and rakip.skor > a.skor * cfg.esik_bandi + cfg.esik_mutlak:
            continue                      # bant asildi -> yerini bosalt
        secilen.append(a)
        kalan.remove(a)
    for a in kalan:
        if len(secilen) >= cfg.slot:
            break
        if not _cakisir(a, secilen, cfg):
            secilen.append(a)
    slot_pay = (1 - cfg.taban_agirlik) / cfg.slot
    return secilen, round(cfg.taban_agirlik + (cfg.slot - len(secilen)) * slot_pay, 4)


if __name__ == "__main__":
    from datetime import timedelta

    N = 420

    def seri(gunluk, tohum=1, n=N, oynak=0.004, pay=None):
        """Tohuma gore farkli zikzak: ayni deseni paylasan iki fon korelasyon
        1.0 cikip cesitlendirme testini anlamsizlastiriyor. Ust bit kullaniliyor,
        LCG'nin dusuk biti 2 periyotlu -- tohum ne olursa olsun ayni desen."""
        d0, v, r, out = date(2025, 8, 15), 1.0, tohum, []
        for i in range(n):
            r = (1103515245 * r + 12345) % 2147483648
            v *= 1 + gunluk + (oynak if (r >> 16) & 1 else -oynak)
            out.append((d0 + timedelta(days=i), v, pay(i) if pay else 1_000_000))
        return out

    def fon(kod, gunluk, sinif="Hisse Senedi", yat=50_000, ad=None, tohum=None, pay=None):
        t = tohum if tohum is not None else sum(map(ord, kod))
        return Fon(kod, ad or f"{kod} FONU", sinif, yat, seri(gunluk, t, pay=pay))

    AS_OF = date(2026, 10, 8)          # serinin son gunu
    TABAN = Fon("PP", "PARA PIYASASI", "Para Piyasası", 900_000, seri(0.0010))
    iyi, orta = fon("AAA", 0.0030), fon("BBB", 0.0020, sinif="Kıymetli Maden")

    a = adaylar([iyi, orta], TABAN, AS_OF, nitelikli=False)
    assert [x.kod for x in a] == ["AAA", "BBB"], a
    assert a[0].fazla > 0 and 0.03 <= a[0].volatilite <= 0.60, a[0]
    assert abs(korelasyon(a[0], a[1])) < 0.3, korelasyon(a[0], a[1])

    assert sepet([]) == ([], 1.0)                       # havuz bos -> tamami para piyasasi
    s, pp = sepet(a[:1]);  assert len(s) == 1 and pp == 0.65, (s, pp)
    s, pp = sepet(a);      assert [x.kod for x in s] == ["AAA", "BBB"] and pp == 0.30

    # ---- sepet mantigi: skorun nereden geldigi onemsiz, Aday'i elle kur ----
    def ad_(kod, skor, sinif="Hisse Senedi", g=None):
        return Aday(kod, sinif, 0.5, 0.1, 0.05, skor, 0.0, 0.0, g or {})

    gun = [date(2026, 1, 1) + timedelta(days=i) for i in range(130)]
    ayni = {d: (0.01 if i % 2 else -0.01) for i, d in enumerate(gun)}
    baska = {d: (0.01 if i % 3 else -0.02) for i, d in enumerate(gun)}

    # korelasyon tavani: ayni seriyi izleyen fon ikinci slota giremez, skoru
    # daha yuksek ve sinifi farkli olsa bile.
    x, y = ad_("XXX", 9.0, g=ayni), ad_("YYY", 8.0, "Kıymetli Maden", ayni)
    assert korelasyon(x, y) > 0.99
    assert [a.kod for a in sepet([x, y])[0]] == ["XXX"]
    assert sepet([x, y])[1] == 0.65
    assert [a.kod for a in sepet([x, y], cfg=Config(max_korelasyon=1.1))[0]] == ["XXX", "YYY"]
    # korelasyonu dusukse ayni siniftan olsa da girer
    z = ad_("ZZZ", 8.0, g=baska)
    assert korelasyon(x, z) < 0.8
    assert [a.kod for a in sepet([x, z])[0]] == ["XXX", "ZZZ"]
    # seri yoksa sinif etiketine dusuyor
    assert [a.kod for a in sepet([ad_("P", 9.0), ad_("Q", 8.0)])[0]] == ["P"]
    assert [a.kod for a in sepet([ad_("P", 9.0), ad_("Q", 8.0, "Döviz")])[0]] == ["P", "Q"]

    # esik bandi: 8.0 * 1.05 + 0.15 = 8.55
    elde, az, cok = ad_("ELD", 8.0, "Döviz"), ad_("AZ", 8.5), ad_("COK", 9.0)
    assert sepet([az, elde], mevcut=["ELD"])[0][0].kod == "ELD"      # bant asilmadi
    assert sepet([cok, elde], mevcut=["ELD"])[0][0].kod == "COK"     # asildi
    # havuzdan dusen fon bant aranmadan cikar
    assert [a.kod for a in sepet([cok], mevcut=["ELD"])[0]] == ["COK"]

    # serbest = sadece nitelikli, ozel = herkese kapali
    kapali = [fon("SRB", 0.0030, ad="X PORTFÖY SERBEST FON"),
              fon("OZL", 0.0030, ad="Y PORTFÖY ÖZEL FON")]
    assert adaylar(kapali, TABAN, AS_OF, False) == []
    assert [x.kod for x in adaylar(kapali, TABAN, AS_OF, True)] == ["SRB"]

    assert adaylar([fon("AZ", 0.0030, yat=4_999)], TABAN, AS_OF, False) == []

    # nakit cikisi: getirisi en iyi fon bile para kaciyorsa havuza girmez
    kacan = fon("KAC", 0.0030, pay=lambda i: int(1_000_000 * 0.995 ** i))
    assert nakit_akis(kacan.seri, AS_OF)[0] < Config().min_akis
    assert adaylar([kacan], TABAN, AS_OF, False) == []
    assert adaylar([iyi], TABAN, AS_OF, False)[0].akis == 0.0

    # akisi POZITIF ama yavaslayan fon elenmez: ivme sadece cikista baglayici
    yag = fon("YAG", 0.0030, pay=lambda i: 1_000_000 + (i * 30 if i < N - 21
                                                        else (N - 21) * 30 + (i - N + 21)))
    a_, i_ = nakit_akis(yag.seri, AS_OF)
    assert a_ > 0 > i_, (a_, i_)
    assert [x.kod for x in adaylar([yag], TABAN, AS_OF, False)] == ["YAG"]

    # yavas kanarken hizlanan cikis -> elenir
    kan = fon("KAN", 0.0030, pay=lambda i: 1_000_000 - (i * 5 if i < N - 21
                                                        else (N - 21) * 5 + (i - N + 21) * 50))
    a_, i_ = nakit_akis(kan.seri, AS_OF)
    assert a_ < 0 and i_ < 0, (a_, i_)
    # hizli kanarken yavaslayan cikis -> gecer
    khz = fon("KHZ", 0.0030, pay=lambda i: 1_000_000 - (i * 50 if i < N - 21
                                                        else (N - 21) * 50 + (i - N + 21) * 5))
    a2, i2 = nakit_akis(khz.seri, AS_OF)
    assert a2 < 0 < i2, (a2, i2)
    kodlar = [x.kod for x in adaylar([kan, khz], TABAN, AS_OF, False)]
    assert "KAN" not in kodlar and "KHZ" in kodlar, kodlar

    assert adaylar([iyi], TABAN, AS_OF, False)[0].ivme == 0.0

    print("engine self-check OK")
