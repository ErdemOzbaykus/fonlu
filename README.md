# Fonlu

TEFAS fonlarını tarama/karşılaştırma, KAP bildirimleri ve kişisel portföy takibi.
API-first: tüm veri `/api/*` üzerinden gelir, frontend sadece bir tüketici (ileride
mobil de öyle olacak).

## Kurulum

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Doğrulandı: Python 3.14 + pytefas 0.4.1. 365 günlük seed ≈ 25 dk (TEFAS rate limit'i
dakikada 6 istek) + ≈ 8 dk (KAP); 595.731 fiyat satırı / 2.486 fon / 87.934 KAP bildirimi
/ ~140 MB SQLite. 90 günlük seed ≈ 5 dk ve ~32 MB, ama 6A/1Y getirileri boş kalır.

## Veriyi doldur (bir defalık seed)

TEFAS dakikada 6 istek kabul ediyor ve 90 günlük çekim birkaç dakika sürüyor, o yüzden
veri önce SQLite'a (`fonlu.db`) yazılır; API hiçbir zaman canlı TEFAS'ı beklemez.

```bash
python -m fonlu.store --days 365
```

`--days 90` daha hızlıdır ama fon detayındaki **6A ve 1Y getiri kutuları boş kalır** —
o dönemin verisi hiç çekilmemiş olur. 1A/3A için 90 gün yeterlidir.

Sonrasında günlük artımlı güncelleme (son kayıtlı tarihten bugüne):

```bash
python -m fonlu.store
```

Cron örneği — hafta içi 20:00:
`0 20 * * 1-5 cd /path/to/Fonlu && .venv/bin/python -m fonlu.store`

Aynı işi arayüzdeki **Veriyi güncelle** düğmesi de yapar (`POST /api/refresh`); arka
planda çalışır, ilerlemeyi sol alttaki durum satırı (`GET /api/status`) gösterir.

Statik dosyalar `Cache-Control: no-cache` ile servis ediliyor — `static/` altını
düzenlerken tarayıcı eski JS'i çalıştırmasın diye.

## Çalıştır

```bash
uvicorn fonlu.main:app --reload
```

http://127.0.0.1:8000 → Fonlar / Karşılaştır / Portföy.

## Docker

```bash
docker compose build
```

Test:

```bash
docker compose run --rm fonlu python test_fonlu.py
```

Seed (veri `fonlu-data` volume'üne yazılır, imaja değil):

```bash
docker compose run --rm fonlu python -m fonlu.store --days 365
```

Çalıştır — site doğrudan http://localhost:8000 adresinde açılır:

```bash
docker compose up -d
```

Ayakta mı diye bakmak için (`healthy` yazması sitenin gerçekten cevap verdiği anlamına
gelir, sadece konteynerin ayakta olduğu değil):

```bash
docker compose ps
```

Durdurmak için `docker compose down`.

Notlar:

- **Port 8000 çakışması:** yerelde `uvicorn` çalışıyorsa `compose up` "port is already
  allocated" verir. Ya yerel sunucuyu durdurun ya da `docker-compose.yml`'de portu
  `"8001:8000"` yapın.
- `restart: unless-stopped` sayesinde Docker Desktop yeniden başladığında site kendiliğinden
  geri gelir.

- Veritabanı yolu `FONLU_DB` ile ayarlanıyor; imajda `/data/fonlu.db`, yerelde repo kökü.
  Volume sayesinde `docker compose build` verinizi silmiyor.
- Mevcut bir veritabanında `holdings` tablosu eski şemadaysa (rapor tarihi ve varlık
  bölümü anahtarda değilken) açılışta otomatik düşürülüp yeniden oluşturuluyor. İçeriği
  KAP'tan yeniden üretilebilen bir önbellek; `prices`, `positions` ve `watchlist`
  etkilenmiyor.
- `static/` read-only bind mount edilmiş, frontend düzenlemeleri yeniden build
  gerektirmiyor. Python tarafını değiştirdiğinizde build gerekiyor.
- Konteyner root olmayan `fonlu` kullanıcısıyla çalışıyor. İmaj ~544 MB (pandas + pdfplumber).
- Doğrulandı: test suite, TEFAS seed, KAP çekimi ve PDF'ten hisse çıkarımı konteyner
  içinde çalışıyor (`pdfplumber` için ek sistem paketi gerekmiyor).

## Self-check

```bash
python test_fonlu.py
```

Sabit fiyatlarla geçici bir DB kurup getiri hesabını, filtreleri (tip, sınıf, getiri,
büyüklük), normalize karşılaştırmayı, türetilen varlık sınıfını, KAP join'ini ve portföy
değerlemesini doğrular. TEFAS veya KAP'a istek atmaz.

## Arayüz

- **Fonlar** — dönem ön ayarları (1A/3A/6A/1Y) veya serbest tarih; fon tipi, varlık
  sınıfı, min getiri, min büyüklük ve metin araması. Her satırda fonun portföy
  kompozisyonunu gösteren yığılmış dağılım çubuğu var; sütun başlıkları sıralanabilir.
  Satıra tıklayınca sağdan detay çekmecesi açılır.
- **Detay çekmecesi** — *Genel* (son fiyat, dönem getirisi, yıllık volatilite, max düşüş,
  1A/3A/6A/1Y kutuları, fiyat grafiği), *Varlık dağılımı*, *KAP bildirimleri*.
- **Varlık dağılımı** — donut + grup dağılımı + 54 kalemin Türkçe adlarıyla listesi.
  Yanında **▸** olan satırlar tıklanınca o varlık sınıfının **kalemleri** açılıyor (hangi
  hisse, hangi kira sertifikası) ve önceki ay raporuyla kıyası gösteriliyor. Ayrı bir
  "Hisseler" sekmesi yok: kalem detayı ait olduğu dağılım satırının altında duruyor.
- **Kaydettiklerim** — her fon satırındaki ☆ ile takip listesine ekleyip çıkarın; bu
  görünüm sadece kaydettiklerinizi gösterir ve hepsini tek tıkla karşılaştırmaya taşır.
  Kendi isteğini attığı için Fonlar sekmesindeki filtreden etkilenmez.
- **Karşılaştır** — “Fon seç” aranabilir bir liste açar, kutucukla en fazla 10 fon
  seçilir; grafik başlangıcı 100'e normalize eder.
- **Portföy** — pozisyon ekleme formu, toplam maliyet/değer/kar-zarar ve pozisyon tablosu.

## API

| Endpoint | Açıklama |
|---|---|
| `GET /api/status` | Önbellek durumu, son veri tarihi, çalışan güncelleme |
| `POST /api/refresh?days=` | Arka planda TEFAS + KAP çekimi (`days` yoksa artımlı) |
| `GET /api/funds` | `kind`, `start`, `end`, `q`, `category`, `min_return`, `max_return`, `min_size`, `limit` |
| `GET /api/funds/{kod}` | Fiyat serisi, dönem getirileri, volatilite, max düşüş, dağılım |
| `GET /api/funds/{kod}/kap` | Fonun KAP bildirimleri (önbellekten) |
| `GET /api/funds/{kod}/holdings` | Varlık sınıfına göre gruplu kalemler + TEFAS uyumu + ay kıyası |
| `POST /api/funds/{kod}/holdings` | Son iki KAP PDF'ini indirip kalemleri çıkarır ve önbelleğe yazar |
| `GET /api/kap/{index}/attachments` | Bildirimin PDF ekleri (KAP'a canlı gider) |
| `GET /api/kap/file/{objId}` | Eki temiz PDF olarak servis eder |
| `GET /api/watchlist`, `PUT/DELETE /api/watchlist/{kod}` | Takip listesi |
| `GET /api/compare?codes=A,B` | 100'e normalize edilmiş getiri serileri (max 10 fon) |
| `GET/POST /api/positions`, `DELETE /api/positions/{id}` | Portföy CRUD + değerleme |

## KAP entegrasyonu

`POST kap.org.tr/tr/api/disclosure/funds/byCriteria` (auth yok) bir tarih aralığındaki
tüm fon bildirimlerini döndürüyor ve `fundCode` alanı TEFAS fon koduyla birebir aynı —
ayrı bir eşleme tablosu gerekmiyor.

### Kalem bazlı portföy (KAP PDF çıkarımı)

Bir fonun **hangi kalemleri** tuttuğu (hangi hisse, hangi kira sertifikası) TEFAS'ta yok;
sadece KAP'ın aylık "Portföy Dağılım Raporu" PDF ekindeki portföy değeri tablosunda var.
Bildirimin JSON gövdesi boş bir XBRL formu, veri gerçekten PDF'in içinde.
`fonlu/holdings.py` bu PDF'i indirip ayrıştırıyor:

- KAP'ın dosya endpoint'i PDF'i **Java-serialize edilmiş bir bayt dizisi** içinde
  döndürüyor; içerik `%PDF-` ofsetinden sonra başlıyor. Uygulama dosyayı kendi
  `/api/kap/file/{objId}` ucundan geçirip temiz PDF veriyor (KAP'ın kendi linki tarayıcıda
  sarmalayıcıyı indiriyor).
- Çıkarım talep üzerine çalışıyor (20-40 sn) ve sonuç SQLite'a yazılıyor; aynı PDF'e bir
  daha gidilmiyor. Toplu seed'e konmadı — binlerce PDF indirmek anlamsız.
- **Son iki rapor** birlikte çekiliyor; ay bazlı kıyas ancak önceki ayın raporu da elde
  olursa yapılabiliyor.

**Bölüm eşleştirme sorunu ve çözümü.** PDF tablosu varlık sınıfı başlıklarıyla bölümlere
ayrılmış (HİSSE SENETLERİ, KİRA SERTİFİKALARI, MEVDUAT...), ama bu başlıklar TEFAS'ın
kategorileriyle birebir örtüşmüyor. Ölçülen bir örnek: KAC fonunda TEFAS %85,86 mevduat
bildiriyor, PDF'te aynı varlıklar "KİRA SERTİFİKALARI" başlığı altında %94,53 olarak
duruyor. Bu yüzden **her bölüm gösterilmeden önce TEFAS ile karşılaştırılıyor**:

| Durum | Anlamı | Arayüzde |
|---|---|---|
| `tam` | Fark ≤ %15 (veya 3 puan) | Satır tıklanabilir |
| `kismi` | Çıkan, TEFAS'takinden az | Tıklanabilir + "kısmi çıkarım" notu |
| `eslesmedi` | Çıkan, TEFAS'takinden fazla / TEFAS sıfır | Gösterilmiyor |
| `yok` | Hiç kalem çıkmadı | Gösterilmiyor |

Böylece yanlış eşleşen bölümler hiç sunulmuyor. Doğrulanmış örnek (SUB): hisse `tam`
(%66,22 / TEFAS %71,70), türev `tam`, maden `eslesmedi` → sadece ilk ikisi açılıyor.

**Fon kalemleri hisseden nasıl ayrılıyor.** Raporlar yatırım fonlarını üç farklı yerde
listeleyebiliyor ve üçü de ayrı ele alınıyor:

1. Kendi ana başlığı altında (`N) KATILMA BELGELERİ`).
2. `DİĞER` ana başlığının altında **alt başlıkla**: `Y.Fonu Türk` → yatırım fonu,
   `Borsa Y.Fonu Türk` → BYF. Alt başlık tanınmazsa fonlar `DİĞER`de kalıyordu.
3. Hiç başlık olmadan, doğrudan hisse bloğunun içinde. Bunlar iki yapısal işaretle
   ayrılıyor: Türk fon ISIN'leri **`TRY`** ile başlıyor (hisseler `TRA`/`TRE`,
   kira sertifikaları `TRD`), ISIN yakalanamamışsa ünvanda **"PORTFÖY"** geçiyor.

Ölçüm: örneklemdeki 9 fon/BYF kalemi (HMC'de üç Hedef Portföy fonu, OPH'de dört Osmanlı
Portföy fonu, OTJ'de GMSTR ve ZGOLD) hisse bölümünden çıkıp doğru bölüme taşındı.

Bölüm başlığı **tam eşleşme** ile bulunuyor, "içinde geçiyor mu" ile değil. Sebebi:
fonun kendi adı satıra sığmayıp `HİSSE SENEDİ YÖNETİMİ` / `(HİSSE SENEDİ` gibi parçalara
bölünüyor ve alt arama bunları hisse başlığı sanıp bölümü değiştiriyordu — PBR raporunda
5 fondan 4'ü bu yüzden hisse bölümüne dağılmıştı.

Diğer ayrıştırıcı kararları:

- **Kod ile ad bitişik olabiliyor** (`PA2-PUSULA PORTFÖY ALTIN...`). Yalnızca boşluk
  kabul etmek bu satırları komple düşürüyordu.
- **Tekrar eden sayfa başlıkları eleniyor.** Raporun kendi adı her sayfada tekrar ediyor
  ve bir önceki kalemin ünvanına yapışıyordu; içinde "PORTFÖY" geçtiği için gerçek bir
  hisse (GÜNDOĞDU GIDA) fon sanılıyordu. Bölüm başlıkları bu elemeden muaf.

- Tek kalemde %100'ü aşan ağırlıklar atılıyor; bunlar portföy tablosu değil
  temettü/alım-satım tablosu satırları oluyor.
- Bazı raporlarda ihraççı unvanı hücreye dikey sığdırıldığı için metin katmanında
  karışıyor (`A E S LE E N K L S` gibi). Böyle unvanlar bilerek boş bırakılıp yerine ISIN
  gösteriliyor — bozuk metin göstermektense hiç göstermemek doğru. Kalemin kimliği zaten
  BIST kodu ya da ISIN.
- Hisse satırı BIST koduyla, tahvil/kira sertifikası satırı ISIN ile başlıyor; kalemin
  kimliği buna göre seçiliyor.

### Ay bazlı kıyas

İki rapor varsa her kalem önceki ayla karşılaştırılıyor: `prev_weight_pct`, `delta` ve
`status` (`yeni` / `mevcut` / `cikti`). Portföyden tamamen çıkmış kalemler de listede
kalıyor, sessizce düşürülmüyor. Doğrulanmış örnek (SUB, Temmuz→Ağustos 2026): PCILT ve
METEN yeni girmiş, BETAE çıkmış, TUPRS 5,03 → 2,79 (−2,24 puan).

## Bilinçli sadeleştirmeler

- **Varlık sınıfı fon adından değil dağılımdan türetiliyor.** Bu daha doğru: örneğin ZJR
  adı "hisse senedi yoğun fon" ama 2026-08-14 itibarıyla portföyünün %100'ü ters repo —
  tablo onu "Para Piyasası" olarak gösteriyor.
- **Kaldıraç korunuyor.** Kaldıraçlı fonlarda TEFAS negatif bir para piyasası bacağı
  bildiriyor (ör. PYI: %123,73 hisse / −%23,73 BİST para piyasası). Negatif kalem
  silinmiyor; çubuk pozitif toplama göre ölçekleniyor, negatif bacak listede ve tooltip'te
  gösteriliyor.
- **Dönem getirisi TEFAS'la aynı şekilde çapalanıyor.** "1 ay önce" = önceki ayın aynı
  günü (30 takvim günü değil), ve o gün fiyat yoksa **öncesindeki** son işlem gününün
  fiyatı esas alınır. Bir sonraki işlem gününe atlamak dönemi kısaltıp getiriyi
  yükseltiyordu (PKU 1A: hatalı %36,97 → doğru %40,33). Aynı çapa tarama tablosundaki
  aralık getirisi için de geçerli, böylece tablo ile detay ekranı birbiriyle tutarlı.
- **Tarama tablosu ilk 250 satırı çiziyor**, "devamını yükle" ile artıyor. Sıralama ve
  filtre her zaman tüm kümeye uygulanıyor; 2.469 satırın tamamını DOM'a basmak her filtre
  değişiminde ~4 sn sürüyordu.
- **Dönem getirisi ancak önbellek o dönemi kapsıyorsa yazılıyor.** 90 günlük seed'de 6A/1Y
  boş kalır — "elimizdeki kadarı" 6 aylık getiri diye sunulmaz. Kapsama kontrolünde 7
  günlük tolerans var: dönemin kesim tarihi hafta sonuna veya tatile denk geldiğinde TEFAS
  o gün fiyat yayınlamıyor, bu birkaç günlük sapma dönemi geçersiz saymamalı. Fonun kendi
  geçmişi kısaysa (yeni ihraç) o dönem yine boş kalır ve bu doğrudur.
- **Volatilite ve max düşüş** günlük getirilerin örneklem std sapması × √252. Fonları
  birbirine göre sıralamak için yeterli; TEFAS'ın resmi risk değeri pytefas'ta yok.
- **Portföy dağılımı sadece güncel.** `breakdown` tablosunda fon başına tek satır. Geçmiş
  istenirse PK'yı `(fund_code, date)` yapıp seed'de aralık çekmek yeterli.
- **pytefas 0.4.1'de `columns="breakdown"` çağrısı `fund_code` filtresini yoksayıyor** —
  tüm fonları döndürüyor. Seed bunu bilerek toplu çekip kod bazında ayırıyor.
- **KAP'ın 2000 kayıt sınırı** ve sayfalama parametresi yok; `kap.fetch_complete` sınıra
  takılan pencereyi ikiye bölerek yeniden deniyor (ay sonlarında bildirim yoğunlaşıyor).
- **Tek kullanıcı.** Auth yok, `positions` tablosunda `user_id` yok.

## Yapılmadı

- Kullanıcı bazlı auth
- Bazı kurucuların PDF düzeni hâlâ okunamıyor (ölçülen örneklemde GNS, GOP, MAC).
  Her kurucunun düzenini kovalamak yerine eşleşmeyen bölümü hiç göstermemek tercih edildi;
  yeni bir düzen gerekirse `fonlu/holdings.py` içindeki `SECTIONS` ve `_parse_row`
  genişletilir.
- Hisse dışı bölümlerin eşleşme oranı düşük. Hisse bölümü örneklemde güvenilir; kira
  sertifikası / mevduat gibi bölümlerde PDF başlıkları TEFAS kategorileriyle sık sık
  çelişiyor ve o satırlar bilinçli olarak açılmıyor.
- Kalem geçmişi iki raporla sınırlı. `holdings` PK'sı `report_date` içerdiği için şema
  daha fazlasını taşıyabilir; çıkarım bilerek son iki raporu çekiyor (ay kıyası için
  yeterli). Daha uzun geçmiş istenirse `LIMIT 2` artırılır.
- Fonun tuttuğu hisselere ait şirket bildirimlerini fon detayında göstermek. Artık
  fon→hisse eşlemesi elimizde olduğu için bu kurulabilir hale geldi; henüz yapılmadı.
