# Fonlu

TEFAS fonlarını tarama/karşılaştırma, KAP bildirimleri ve kişisel portföy takibi.
API-first: tüm veri `/api/*` üzerinden gelir, frontend sadece bir tüketici (ileride
mobil de öyle olacak).

Veri Supabase Postgres'te, hesaplar Supabase Auth'ta durur. Uygulama **yalnızca Docker'da**
çalışır; host'ta Python kurulumu gerekmiyor.

## Kurulum

`.env.example`'ı kopyalayıp iki değeri doldurun:

```bash
cp .env.example .env
```

| Değişken | Ne |
|---|---|
| `DATABASE_URL` | Supabase session pooler DSN'i, `fonlu_app` rolü ve şifresiyle |
| `SUPABASE_URL` | Proje API URL'i; JWT doğrulaması bunun JWKS ucundan yapılır |

`.env` git'e girmez. Ardından:

```bash
docker compose up -d --build
```

http://localhost:8000 → giriş ekranı. Kayıt olup giriş yapınca Fonlar / Karşılaştır /
Portföy sekmeleri açılır. Bütün `/api/*` uçları giriş ister; token'sız istek `401` döner.

Ayakta mı diye bakmak için (`healthy` yazması sitenin gerçekten cevap verdiği anlamına
gelir, sadece konteynerin ayakta olduğu değil):

```bash
docker compose ps
```

Durdurmak için `docker compose down`.

### Hazır imajı başka makinede çalıştırma

`docker push`/`pull` sadece imajı taşır; `.env` (şifre içerdiği için) imaja hiç
girmiyor. Apple Silicon'da build edilen imaj arm64 olur ve amd64 makinede `no matching
manifest` der; Windows/Linux PC için imajı Mac'te şöyle üret:

```bash
docker build --platform linux/amd64 -t kullaniciadi/fonlu:latest . && docker push kullaniciadi/fonlu:latest
```

Pull ettiğin makinede şablonu imajın içinden çıkarıp elle doldur:

```bash
docker create --name fonlu-tmp kullaniciadi/fonlu:latest && docker cp fonlu-tmp:/app/.env.example .env && docker rm fonlu-tmp
```

(`kullaniciadi/fonlu:latest` yerine kendi imaj adin; `docker images` ile bakabilirsin.
Docker imaj adlari kucuk harf olmak zorunda.)

`.env`'i doldurduktan sonra:

```bash
docker run -d --name fonlu --env-file .env -p 8000:8000 kullaniciadi/fonlu:latest
```

Değişkenler eksikse konteyner açılışta `DATABASE_URL tanimli degil` diyip duruyor —
`docker logs fonlu` ile görünür.

### Otomatik dağıtım

Mac'te tek komut derleyip Hub'a atar:

```bash
./scripts/deploy.sh
```

Başka imaj adı için `FONLU_IMAGE=kullanici/fonlu:latest ./scripts/deploy.sh`.

Windows tarafında pull'u Watchtower yapıyor. Bir kez kurulur, sonra her push
kendiliğinden inip konteyneri yeniler (env ve port ayarları korunur):

```bash
docker run -d --name watchtower --restart unless-stopped -v /var/run/docker.sock:/var/run/docker.sock -e REPO_USER=kullanici -e REPO_PASS=<hub-access-token> containrrr/watchtower --cleanup --interval 300 fonlu
```

`REPO_USER`/`REPO_PASS` repo private olduğu için gerekiyor; token'ı Hub'da
Account Settings → Personal access tokens'tan read-only üretin. Docker
Desktop'ta `~/.docker/config.json`'ı mount etmek yetmez, o dosya şifreyi
tutmuyor (`credsStore: desktop`). Repo public ise ikisini de silin.

Sondaki `fonlu` sadece o konteyneri izler; makinedeki başka konteynerlere
dokunmaz. `--cleanup` eski imaj katmanlarını siler, disk şişmez.

### Aynı ağdaki başka cihazdan

Konteyner zaten `0.0.0.0:8000`'e bağlı, ekstra ayar yok. Telefon/tablet aynı Wi-Fi'daysa:

```
http://host.local:8000
```

Ekran 700 px'in altındaysa arayüz telefon düzenine geçer: sol ray alta sekme
çubuğu olur (durum/güncelle/çıkış `⋯` altında), tablo satırları kart olur, fon
listesinin başlık satırı yatay kaydırılan sıralama şeridine döner.

`.local` adı çözülmezse (Android bazen çözemiyor) Mac'in IP'sini kullanın:
`ipconfig getifaddr en0` → `http://192.168.1.12:8000`. IP DHCP ile değişebilir, `.local` değişmez.

## Veriyi doldur ve güncelle

TEFAS dakikada 6 istek kabul ediyor ve 90 günlük çekim birkaç dakika sürüyor, o yüzden
veri önce önbelleğe yazılır; API hiçbir zaman canlı TEFAS'ı beklemez.

```bash
docker compose exec fonlu python -m fonlu.store --days 90
```

`--days 365` fon detayındaki 6A ve 1Y getiri kutularını da doldurur ama ≈ 25 dk (TEFAS)
+ ≈ 8 dk (KAP) sürer. 90 gün 1A/3A için yeterlidir; **6A ve 1Y kutuları boş kalır** —
o dönemin verisi hiç çekilmemiş olur.

`--days` verilmezse son kayıtlı tarihten bugüne artımlı güncelleme yapılır:

```bash
docker compose exec fonlu python -m fonlu.store
```

Aynı işi arayüzdeki **Veriyi güncelle** düğmesi de yapar (`POST /api/refresh`); arka
planda çalışır, ilerlemeyi sol alttaki durum satırı (`GET /api/status`) gösterir.
Önbellek ortaktır: bir kullanıcının tetiklediği güncelleme herkese yarar. Kullanıcıya
özel olan yalnızca watchlist ve pozisyonlardır.

## Self-check

```bash
docker compose --profile test run --rm tests
```

`test` profili yanına tek kullanımlık bir Postgres (`test-db`, tmpfs) kaldırır ve her
koşum kendi geçici şemasında çalışır. Sabit fiyatlarla getiri hesabını, filtreleri (tip,
sınıf, getiri, büyüklük), normalize karşılaştırmayı, türetilen varlık sınıfını, KAP
join'ini, portföy değerlemesini ve kullanıcı izolasyonunu doğrular. TEFAS, KAP veya
Supabase'e istek atmaz.

## Notlar

- **Port 8000 çakışması:** başka bir şey 8000'i tutuyorsa `compose up` "port is already
  allocated" verir; `docker-compose.yml`'de portu `"8001:8000"` yapın.
- `restart: unless-stopped` sayesinde Docker Desktop yeniden başladığında site kendiliğinden
  geri gelir.
- Adlandırılmış volume yok — tüm durum Supabase'de, imaj tamamen tek kullanımlık.
  `docker compose down` veri kaybetmez.
- `static/` read-only bind mount edilmiş, frontend düzenlemeleri yeniden build
  gerektirmiyor; statik dosyalar `Cache-Control: no-cache` ile servis ediliyor. Python
  tarafını değiştirdiğinizde build gerekiyor.
- Konteyner root olmayan `fonlu` kullanıcısıyla çalışıyor. İmaj ~544 MB (pandas + pdfplumber).
- Doğrulandı: test suite, TEFAS seed, KAP çekimi ve PDF'ten hisse çıkarımı konteyner
  içinde çalışıyor (`pdfplumber` için ek sistem paketi gerekmiyor).

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
- Çıkarım talep üzerine çalışıyor (20-40 sn) ve sonuç önbelleğe yazılıyor; aynı PDF'e bir
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
- **RLS politikası yok.** İzolasyon şema + rol yetkisi + sorgulardaki `WHERE user_id = %s`
  ile sağlanıyor: tablolar PostgREST'e açık olmayan `fonlu` şemasında duruyor ve uygulama
  yalnız o şemaya yetkili `fonlu_app` rolüyle bağlanıyor.

## Yapılmadı

- Şifre sıfırlama ve e-posta doğrulama akışı (Supabase'de e-posta onayı kapalı).
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
