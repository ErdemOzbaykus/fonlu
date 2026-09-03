# Fonlu

TEFAS yatırım fonları için tarama, karşılaştırma ve portföy takip terminali.
Fon fiyatlarını ve portföy dağılımlarını TEFAS'tan, fon bildirimlerini KAP'tan
toplar; KAP'ın aylık portföy dağılım raporu PDF'lerinden fonun **hangi hisseyi
ne kadar tuttuğunu** çıkarır — bu bilgi TEFAS'ta yok.

Kendi kendine barındırılır (self-hosted): tek Docker konteyneri + kendi Supabase
projeniz. Ortak bir sunucu ya da üçüncü taraf servis yok.

## Ne yapar

- **Tarama** — 2.400+ fonu dönem getirisi, fon tipi, varlık sınıfı, büyüklük ve
  metin aramasıyla filtreleyip sıralar.
- **Fon detayı** — fiyat grafiği, 1A/3A/6A/1Y getirileri, yıllık volatilite, maksimum
  düşüş, varlık dağılımı donut'u ve fonun KAP bildirimleri.
- **Kalem bazlı portföy** — KAP PDF'inden çıkarılan gerçek kalemler (hangi hisse,
  hangi kira sertifikası) ve bir önceki aya göre değişim.
- **Karşılaştırma** — en fazla 10 fonun 100'e normalize edilmiş getiri grafiği.
- **Portföy** — pozisyon girişi, güncel değerleme, kar/zarar.
- **Takip listesi** — kullanıcıya özel, filtrelerden bağımsız.

Arayüz masaüstü ve telefon için tek DOM üzerinden çalışır; 700 px altında sol ray
alt sekme çubuğuna, tablolar kartlara dönüşür.

## Mimari

| Katman | Seçim |
|---|---|
| Backend | FastAPI + psycopg3 (bağlantı havuzu) |
| Veritabanı | Supabase Postgres, session pooler üzerinden |
| Kimlik | Supabase Auth — şifre veya passkey (WebAuthn) |
| Frontend | Bağımlılıksız vanilla JS + Chart.js; derleme adımı yok |
| Çalıştırma | Yalnızca Docker; host'ta Python kurulumu gerekmez |

API-first: tüm veri `/api/*` üzerinden gelir, frontend yalnızca bir tüketicidir.

Yetkilendirme modeli: tablolar PostgREST'e açılmayan `fonlu` şemasında durur ve
uygulama yalnız o şemaya yetkili `fonlu_app` rolüyle bağlanır. Frontend, Supabase
Auth'tan aldığı ES256 imzalı JWT'yi `Authorization` başlığında yollar; backend
token'ı JWKS ile doğrulayıp `sub` claim'ini `user_id` olarak kullanır. Kullanıcıya
özel her sorguda `WHERE user_id = %s` vardır. Bu yüzden RLS politikası yazılmamıştır
— izolasyon şema + rol yetkisi + sorgu katmanında sağlanır.

## Kurulum

Gereken: Docker ve bir Supabase projesi (ücretsiz katman yeterli).

### 1. Supabase projesini hazırlayın

Yeni bir proje oluşturun ve şu iki değeri not edin (Project Settings → API):

- **Project URL** — `https://<ref>.supabase.co`
- **Publishable (anon) key** — `sb_publishable_...`

Publishable key tarayıcıya gömülmek üzere tasarlanmıştır, gizli değildir.

### 2. Şemayı ve uygulama rolünü oluşturun

SQL Editor'de sırayla çalıştırın. Önce şema — `<schema.sql içeriği>` yerine bu
repodaki [`fonlu/schema.sql`](fonlu/schema.sql) dosyasının tamamını yapıştırın:

```sql
CREATE SCHEMA IF NOT EXISTS fonlu;
SET search_path = fonlu;

-- <schema.sql içeriği>

-- PostgREST yalnızca expose edilen şemaları servis eder; fonlu listede değildir.
-- Yine de yetkiyi açıkça geri alıyoruz: şema ileride yanlışlıkla expose edilse
-- bile bu iki rol okuyamasın.
REVOKE ALL ON SCHEMA fonlu FROM anon, authenticated;
REVOKE ALL ON ALL TABLES IN SCHEMA fonlu FROM anon, authenticated;
```

Sonra rol. `<güçlü-bir-şifre>` yerine kendi ürettiğiniz şifreyi koyun; birazdan
`DATABASE_URL`'e girecek:

```sql
CREATE ROLE fonlu_app WITH LOGIN PASSWORD '<güçlü-bir-şifre>';

GRANT USAGE ON SCHEMA fonlu TO fonlu_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA fonlu TO fonlu_app;
-- positions.id bir identity sütunu; sequence yetkisi olmadan INSERT patlar.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA fonlu TO fonlu_app;

-- Uygulama bağlantısı search_path'i kendi ayarlamak zorunda kalmasın.
ALTER ROLE fonlu_app SET search_path = fonlu;

-- Hesap silindiğinde o hesaba ait satırlar da gitsin. Yerel test Postgres'inde
-- auth şeması olmadığı için bu kısıt schema.sql'de değil, burada.
ALTER TABLE fonlu.watchlist
    ADD CONSTRAINT watchlist_user_fk FOREIGN KEY (user_id)
    REFERENCES auth.users(id) ON DELETE CASCADE;
ALTER TABLE fonlu.positions
    ADD CONSTRAINT positions_user_fk FOREIGN KEY (user_id)
    REFERENCES auth.users(id) ON DELETE CASCADE;
```

### 3. Hesapları kapatın ve kendinizi ekleyin

Uygulamada kayıt ekranı **yoktur** — bu bilinçli bir tercihtir; adresi bilen
herkesin hesap açmasını engeller.

1. Authentication → Sign In / Providers → **Allow new users to sign up** kapalı.
2. Authentication → URL Configuration → **Site URL**'i uygulamanın gerçek
   adresi yapın (varsayılan `http://localhost:3000`, davet linkleri oraya
   gider ve açılmaz). Aynı adresi **Redirect URLs**'e de ekleyin.
3. Authentication → Users → **Invite user** ile kendinizi ve paylaşacağınız
   kişileri ekleyin.

Davet edilen kişi linke tıklayınca uygulamaya oturum açmış olarak düşer
(oturum URL fragment'ından okunup adres çubuğundan silinir). Şifresi yoktur;
ilk iş olarak **Passkey ekle** demesi gerekir, sonraki girişler passkey ile
olur. Passkey'ini kaybederse panelden yeni davet gönderin.

### 4. Passkey (isteğe bağlı)

Şifresiz giriş için Authentication → Passkeys:

- **Enable Passkey authentication** açık
- **Relying Party ID** — uygulamayı açtığınız çıplak alan adı (`ornek.com`).
  Şema, port ve yol yazılmaz.
- **Relying Party Origins** — `https://ornek.com`

Uyarılar:

- WebAuthn güvenli bağlam ister: **HTTPS**, ya da `localhost`. Düz HTTP'de bir
  LAN IP'si üzerinden passkey çalışmaz, RP ID olarak IP de kabul edilmez.
- RP ID değişirse kayıtlı tüm passkey'ler geçersiz olur. Kullanıcılar passkey
  eklemeye başlamadan önce sabitleyin.
- Passkey desteği Supabase tarafında halen deneyseldir.

Kullanıcı, giriş yaptıktan sonra sol alttaki **Passkey ekle** ile cihazını
kaydeder; sonraki girişlerde **Passkey ile giriş** yeterlidir.

### 5. `.env` dosyasını doldurun

```bash
cp .env.example .env
```

| Değişken | Değer |
|---|---|
| `DATABASE_URL` | Session pooler DSN'i, `fonlu_app` rolü ve şifresiyle |
| `SUPABASE_URL` | `https://<ref>.supabase.co` — JWT doğrulaması bunun JWKS ucundan yapılır |

DSN şu biçimdedir; `aws-<n>` öneki projeden projeye değişir, panelin **Connect**
ekranından doğrulayın:

```
postgresql://fonlu_app.<ref>:<şifre>@aws-0-eu-central-1.pooler.supabase.com:5432/postgres
```

Bağlantıyı sınayın — beklenen çıktı `fonlu_app | fonlu`:

```bash
docker run --rm postgres:17 psql "$DATABASE_URL" -c "select current_user, current_schema()"
```

`.env` git'e girmez.

### 6. Frontend sabitlerini kendi projenize çevirin

[`static/app.js`](static/app.js) başındaki iki satır kendi projenizi göstermeli:

```js
const SUPABASE_URL = "https://<ref>.supabase.co";
const SUPABASE_KEY = "sb_publishable_...";
```

Bu değerler tarayıcıya gömülür ve gizli değildir; koruma, kapalı kayıt ve
kullanıcı başına yetkilendirmedir.

### 7. Çalıştırın

```bash
docker compose up -d --build
```

http://localhost:8000 → giriş ekranı. Bütün `/api/*` uçları giriş ister;
token'sız istek `401` döner.

Sitenin gerçekten cevap verdiğini görmek için (`healthy`, yalnızca konteynerin
ayakta olması değil):

```bash
docker compose ps
```

Durdurmak için `docker compose down`.

## Veriyi doldurma ve güncelleme

TEFAS dakikada 6 istek kabul ediyor ve 90 günlük çekim birkaç dakika sürüyor; bu
yüzden veri önce önbelleğe yazılır, API hiçbir zaman canlı TEFAS'ı beklemez.

İlk doldurma:

```bash
docker compose exec fonlu python -m fonlu.store --days 90
```

`--days 365`, fon detayındaki 6A ve 1Y getiri kutularını da doldurur ama ≈ 25 dk
(TEFAS) + ≈ 8 dk (KAP) sürer. 90 gün 1A/3A için yeterlidir; **6A ve 1Y kutuları
boş kalır** — o dönemin verisi hiç çekilmemiş olur.

`--days` verilmezse son kayıtlı tarihten bugüne artımlı güncelleme yapılır:

```bash
docker compose exec fonlu python -m fonlu.store
```

Aynı işi arayüzdeki **Veriyi güncelle** düğmesi de yapar (`POST /api/refresh`);
arka planda çalışır, ilerlemeyi sol alttaki durum satırı (`GET /api/status`)
gösterir. Önbellek ortaktır: bir kullanıcının tetiklediği güncelleme herkese
yarar. Kullanıcıya özel olan yalnızca takip listesi ve pozisyonlardır.

## Testler

```bash
docker compose --profile test run --rm tests
```

`test` profili yanına tek kullanımlık bir Postgres (`test-db`, tmpfs) kaldırır ve
her koşum kendi geçici şemasında çalışır. Sabit fiyatlarla getiri hesabını,
filtreleri, normalize karşılaştırmayı, türetilen varlık sınıfını, KAP join'ini,
portföy değerlemesini ve kullanıcı izolasyonunu doğrular. TEFAS, KAP veya
Supabase'e istek atmaz.

## Başka bir makinede çalıştırma

`docker push`/`pull` yalnızca imajı taşır; `.env` şifre içerdiği için imaja hiç
girmez. Apple Silicon'da üretilen imaj arm64 olur ve amd64 makinede `no matching
manifest` der, bu yüzden hedef mimari açıkça verilir:

```bash
FONLU_IMAGE=<kullanici>/fonlu:latest ./scripts/deploy.sh
```

Hedef makinede `.env` şablonunu imajın içinden çıkarıp doldurun:

```bash
docker create --name fonlu-tmp <kullanici>/fonlu:latest \
  && docker cp fonlu-tmp:/app/.env.example .env \
  && docker rm fonlu-tmp
```

Sonra:

```bash
docker run -d --name fonlu --env-file .env -p 8000:8000 <kullanici>/fonlu:latest
```

Değişkenler eksikse konteyner açılışta `DATABASE_URL tanimli degil` der;
`docker logs fonlu` ile görünür. Docker imaj adları küçük harf olmak zorundadır.

### Otomatik dağıtım

Hedef makinede pull'u [Watchtower](https://containrrr.dev/watchtower/) yapar. Bir
kez kurulur, sonra her push kendiliğinden iner ve konteyneri yeniler — env ve port
ayarları korunur:

```bash
docker run -d --name watchtower --restart unless-stopped \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e DOCKER_API_VERSION=1.44 \
  -e REPO_USER=<kullanici> -e REPO_PASS=<hub-access-token> \
  containrrr/watchtower --cleanup --interval 300 fonlu
```

- Sondaki `fonlu` yalnızca o konteyneri izler; makinedeki diğer konteynerlere
  dokunmaz. `--cleanup` eski imaj katmanlarını siler.
- `REPO_USER`/`REPO_PASS` yalnızca imaj deposu private ise gerekir. Docker
  Desktop'ta `~/.docker/config.json`'ı mount etmek yetmez, o dosya şifreyi tutmaz
  (`credsStore: desktop`); Hub'da Account Settings → Personal access tokens'tan
  read-only bir token üretin.
- `DOCKER_API_VERSION` şarttır: watchtower imajı bakımsızdır, Docker API'sini
  varsayılan 1.25 ile konuşur ve Docker 29 `client version 1.25 is too old` der.

## Uzaktan erişim

Konteyner `0.0.0.0:8000`'e bağlıdır. Aynı Wi-Fi'daki bir cihazdan host'un `.local`
adı ya da IP'si yeterlidir (`http://<host>.local:8000`).

Dışarıdan erişim için [Tailscale](https://tailscale.com) pratik bir yol; HTTPS
sertifikasını da o üstlenir, bu da passkey'in gerektirdiği güvenli bağlamı sağlar:

```bash
tailscale serve --bg 8000     # yalnızca kendi tailnet'inizden
tailscale funnel --bg 8000    # herkese açık internetten
```

`serve` adresini yalnızca tailnet'e bağlı cihazlar çözebilir. Tailscale
kuramayacak kişilerle paylaşacaksanız `funnel` gerekir — o zaman giriş ekranı
herkese açılır, kapalı kaydı doğrulamış olmanız önemlidir.

Her iki durumda da Supabase'deki **Relying Party ID**'yi bu hostname'e ayarlayın.

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
- **Karşılaştır** — "Fon seç" aranabilir bir liste açar, kutucukla en fazla 10 fon
  seçilir; grafik başlangıcı 100'e normalize eder.
- **Portföy** — pozisyon ekleme formu, toplam maliyet/değer/kar-zarar ve pozisyon tablosu.
- **Bildirimler** — takip edilen fonların KAP bildirimleri, okundu işaretiyle.

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
- **JWT doğrulamasında 60 sn saat toleransı.** Konteynerin saati Supabase'inkinden birkaç
  saniye geri kalınca taze token `The token is not yet valid (iat)` ile reddediliyordu.

## Yapılmadı

- Kayıt, şifre sıfırlama ve e-posta doğrulama akışları. Hesaplar Supabase panelinden
  davetle açılıyor.
- Passkey listeleme/silme/yeniden adlandırma ekranı. Supabase API'si destekliyor
  (`auth.passkey.list/update/delete`), arayüz henüz yok.
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

## Sorun giderme

| Belirti | Sebep / çözüm |
|---|---|
| `port is already allocated` | 8000 doluysa `docker-compose.yml`'de `"8001:8000"` yapın |
| `DATABASE_URL tanimli degil` | `.env` eksik ya da `--env-file` verilmemiş |
| `Tenant or user not found` | DSN'deki `aws-<n>` öneki yanlış; Connect ekranından doğrulayın |
| `no matching manifest` | İmaj arm64, hedef amd64 — `deploy.sh` ile üretin |
| `Oturum gecersiz: ... (iat)` | Konteyner saati kaymış; kayma dakikalarcaysa Docker'ı yeniden başlatın |
| Passkey butonu hata veriyor | HTTPS değilsiniz, ya da RP ID adresle uyuşmuyor |

## Notlar

- Adlandırılmış volume yok — tüm durum Supabase'de, imaj tamamen tek kullanımlık.
  `docker compose down` veri kaybettirmez.
- `static/` read-only bind mount edilmiş, frontend düzenlemeleri yeniden build
  gerektirmiyor; statik dosyalar `Cache-Control: no-cache` ile servis ediliyor. Python
  tarafını değiştirdiğinizde build gerekiyor.
- Konteyner root olmayan `fonlu` kullanıcısıyla çalışıyor. İmaj ~544 MB (pandas +
  pdfplumber).
- `restart: unless-stopped` sayesinde Docker yeniden başladığında site kendiliğinden
  geri gelir.

## Sorumluluk reddi

Fonlu bir kişisel takip aracıdır, yatırım tavsiyesi değildir. Veriler TEFAS ve KAP'ın
kamuya açık uçlarından toplanır; doğruluğu ya da güncelliği garanti edilmez.
