const $ = (id) => document.getElementById(id);

// Anon key ve URL frontend'e gömülüyor; ikisi de gizli değil, tasarım gereği.
const SUPABASE_URL = "https://phmewfvayruiwxnbmvrp.supabase.co";
const SUPABASE_KEY = "sb_publishable_eeQgH8QQZY8J3FuaHug5iw_cVj3wxyt";
const AUTH = SUPABASE_URL + "/auth/v1";

const session = {
  get: () => JSON.parse(localStorage.getItem("fonlu-session") || "null"),
  set: (s) => localStorage.setItem("fonlu-session", JSON.stringify(s)),
  clear: () => localStorage.removeItem("fonlu-session"),
};

async function gotrue(path, body) {
  const r = await fetch(AUTH + path, {
    method: "POST",
    headers: { "Content-Type": "application/json", apikey: SUPABASE_KEY },
    body: JSON.stringify(body),
  });
  const d = await r.json();
  if (!r.ok) throw new Error(d.error_description || d.msg || d.message || "Giriş başarısız");
  return d;
}

const signIn = (email, password) =>
  gotrue("/token?grant_type=password", { email, password }).then(session.set);

// Passkey: WebAuthn toreni (base64url donusumleri, navigator.credentials)
// supabase-js'te hazir; elde yazmak ~60 satir kodlama yamasi demekti.
// Kutuphane kendi oturum kabini tutmasin, oturum yine yukaridaki session'da.
const sb = supabase.createClient(SUPABASE_URL, SUPABASE_KEY, {
  auth: {
    persistSession: false,
    autoRefreshToken: false,
    experimental: { passkey: true },
  },
});

async function passkeySignIn() {
  const { data, error } = await sb.auth.signInWithPasskey();
  if (error) throw error;
  session.set(data.session);
}

// Passkey kaydi giris yapmis kullanici ister; token'i kutuphaneye devret.
async function passkeyRegister() {
  const s = session.get();
  await sb.auth.setSession({
    access_token: s.access_token,
    refresh_token: s.refresh_token,
  });
  const { error } = await sb.auth.registerPasskey();
  if (error) throw error;
}

// Davetle gelen hesabin sifresi yok; passkey kaydetmeden cikis yaparsa
// kilitlenip yeni davet bekliyordu. GoTrue'nun /user ucu oturum acikken
// sifre belirlemeye izin veriyor.
async function updatePassword(password) {
  const r = await fetch(AUTH + "/user", {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      apikey: SUPABASE_KEY,
      Authorization: `Bearer ${session.get().access_token}`,
    },
    body: JSON.stringify({ password }),
  });
  const d = await r.json();
  if (!r.ok) throw new Error(d.error_description || d.msg || d.message || "Şifre güncellenemedi");
}

// Access token 1 saatte doluyor; refresh token'la sessizce yenile.
async function refresh() {
  const s = session.get();
  if (!s?.refresh_token) return null;
  try {
    const fresh = await gotrue("/token?grant_type=refresh_token",
                               { refresh_token: s.refresh_token });
    session.set(fresh);
    return fresh.access_token;
  } catch {
    session.clear();
    return null;
  }
}

// Perde ancak is 250 ms'yi asarsa aciliyor: onbellekten donen 80 ms'lik
// istekte ekranin bir anlik parlamasi, beklemeden daha rahatsiz edici.
let busyN = 0, busyTimer;
async function busy(work) {
  // Sayac sart: acilis kendi icinde loadFunds'i cagiriyor, ic cagrinin bitisi
  // dis cagri surerken perdeyi indirmemeli.
  if (busyN++ === 0) busyTimer = setTimeout(() => $("loading").hidden = false, 250);
  try { return await work; } finally {
    if (--busyN === 0) { clearTimeout(busyTimer); $("loading").hidden = true; }
  }
}

const api = async (path, opts = {}, retry = true) => {
  const s = session.get();
  const headers = { ...opts.headers };
  if (s?.access_token) headers.Authorization = `Bearer ${s.access_token}`;
  const r = await fetch("/api" + path, { ...opts, headers });
  // Tek sefer yenilemeyi dene; yine 401 ise oturum gercekten bitmis.
  if (r.status === 401 && retry) {
    if (await refresh()) return api(path, opts, false);
    session.clear();
    showAuthGate();
    throw new Error("Oturum sona erdi, tekrar giriş yapın.");
  }
  // opts.blob: PDF gibi ikili yanitlar. Duz <a href> Authorization basligi
  // tasimadigi icin ekler 401 aliyordu; ayni token'la burdan iniyorlar.
  if (r.ok && opts.blob) return r.blob();
  const body = r.status === 204 ? null : await r.json();
  if (!r.ok) throw new Error(body?.detail || r.statusText);
  return body;
};
const esc = (s) => String(s ?? "").replace(/[<>&"]/g, (c) =>
  ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c]));
const num = (v, d = 2) => (v == null ? "—" : v.toLocaleString("tr-TR",
  { minimumFractionDigits: d, maximumFractionDigits: d }));
const compact = (v) => (v == null ? "—" : v.toLocaleString("tr-TR",
  { notation: "compact", maximumFractionDigits: 1 }));
const int = (v) => (v == null ? "—" : v.toLocaleString("tr-TR"));
const num2 = (v) => (v == null ? "—" : v.toLocaleString("tr-TR", { maximumFractionDigits: 2 }));
const cls = (v) => (v == null ? "muted" : v >= 0 ? "up" : "down");
const pct = (v) => (v == null ? "—" : `${v > 0 ? "+" : ""}${num(v)}%`);
const cell = (v) => `<td class="num ${cls(v)}">${pct(v)}</td>`;
const iso = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 6e4).toISOString().slice(0, 10);
// "1 ay önce" = önceki ayın aynı günü. TEFAS dönemleri takvim ayı; 30 gün geriye
// gitmek anchor'ı kaydırıp getiriyi TEFAS'la uyumsuz hale getiriyor.
function monthsBack(isoDate, months) {
  const [y, m, d] = isoDate.split("-").map(Number);
  const total = y * 12 + (m - 1) - months;
  const yy = Math.floor(total / 12), mm = (total % 12) + 1;
  const last = new Date(Date.UTC(yy, mm, 0)).getUTCDate();
  return `${yy}-${String(mm).padStart(2, "0")}-${String(Math.min(d, last)).padStart(2, "0")}`;
}
// Dönemler piyasadaki son veri gününe göre kurulur; bugüne göre değil, yoksa
// hafta sonu / tatilde dönem bir-iki gün kayıyor.
let lastDataDate = iso(new Date());

const GROUPS = ["Hisse Senedi", "Kıymetli Maden", "Para Piyasası", "Borçlanma Aracı",
                "Döviz", "Fon Sepeti", "Diğer"];
const GROUP_HEX = ["#f0a13c", "#d9c46b", "#4f9bd9", "#7a86c9", "#4bbfa8", "#c47ab5", "#5c6480"];

// TEFAS ships the breakdown columns in English; label the ones funds actually use.
const ALLOC_TR = {
  stock: "Hisse senedi", government_bond: "Devlet tahvili", treasury_bill: "Hazine bonosu",
  financing_bill: "Finansman bonosu", private_sector_bond: "Özel sektör tahvili",
  bank_bill: "Banka bonosu", asset_backed_securities: "Varlığa dayalı menkul kıymet",
  eurobond: "Eurobond", repo: "Repo", reverse_repo: "Ters repo", term_deposit: "Vadeli mevduat",
  deposit_tl: "Mevduat (TL)", deposit_fx: "Mevduat (döviz)", deposit_gold: "Mevduat (altın)",
  participation_account: "Katılma hesabı", participation_account_tl: "Katılma hesabı (TL)",
  participation_account_fx: "Katılma hesabı (döviz)", participation_account_gold: "Katılma hesabı (altın)",
  precious_metals: "Kıymetli madenler", precious_metals_etf: "Kıymetli maden BYF",
  precious_metals_government_debt: "Kıymetli maden devlet borçlanma",
  foreign_security: "Yabancı menkul kıymet", foreign_stock: "Yabancı hisse",
  foreign_debt_security: "Yabancı borçlanma aracı", foreign_etf: "Yabancı BYF",
  investment_fund: "Yatırım fonu", etf: "Borsa yatırım fonu", derivative: "Türev araç",
  takasbank_money_market: "Takasbank para piyasası", bist_money_market: "BİST para piyasası",
  government_lease_certificate: "Kira sertifikası", government_lease_certificate_tl: "Kira sertifikası (TL)",
  government_lease_certificate_fx: "Kira sertifikası (döviz)",
  private_sector_lease_certificate: "Özel sektör kira sertifikası",
  fund_participation_certificate: "Fon katılma belgesi",
  futures_cash_collateral: "Vadeli işlem nakit teminatı", real_estate_investment: "Gayrimenkul yatırımı",
  venture_capital_fund: "Girişim sermayesi fonu", other: "Diğer",
  bist_committed_buy: "BİST taahhütlü işlem (alış)",
  bist_committed_sell: "BİST taahhütlü işlem (satış)",
  government_external_debt: "Devlet dış borçlanma aracı",
  private_sector_external_debt: "Özel sektör dış borçlanma aracı",
  foreign_government_debt: "Yabancı kamu borçlanma aracı",
  foreign_private_sector_debt: "Yabancı özel sektör borçlanma aracı",
  fx_government_internal_debt: "Dövize endeksli devlet iç borçlanma aracı",
  government_foreign_lease_certificate: "Kamu yabancı kira sertifikası",
  private_sector_foreign_lease_certificate: "Özel sektör yabancı kira sertifikası",
  precious_metals_lease_certificate: "Kıymetli maden kira sertifikası",
  real_estate_certificate: "Gayrimenkul sertifikası",
  real_estate_fund: "Gayrimenkul yatırım fonu",
};
const allocLabel = (k) => ALLOC_TR[k.replace(/_pct$/, "")]
  || k.replace(/_pct$/, "").replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

// ---------------- charts ----------------
Chart.defaults.color = "#8a93a8";
Chart.defaults.borderColor = "#2c3446";
Chart.defaults.font.family = "ui-monospace, 'SF Mono', Menlo, monospace";
Chart.defaults.font.size = 11;
const LINE_HEX = ["#f0a13c", "#4bbfa8", "#4f9bd9", "#c47ab5", "#e5626b",
                  "#d9c46b", "#7a86c9", "#35c88a"];

const charts = {};
function line(id, labels, datasets, yLabel, yTick) {
  charts[id]?.destroy();
  charts[id] = new Chart($(id), {
    type: "line",
    data: {
      labels,
      datasets: datasets.map((d, i) => ({
        borderColor: LINE_HEX[i % LINE_HEX.length], backgroundColor: LINE_HEX[i % LINE_HEX.length],
        borderWidth: 1.75, pointRadius: 0, pointHitRadius: 12, tension: 0.12,
        ...d,  // dataset'in kendi tipi/rengi kazanir: nakit akisi bar olarak ciziliyor
      })),
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: {
          grid: { display: false },
          ticks: {
            maxTicksLimit: 7, maxRotation: 0, autoSkipPadding: 12,
            // Full ISO dates collide on a 1-year range; '15.08.25' fits.
            callback(i) {
              const [y, m, d] = String(this.getLabelForValue(i)).split("-");
              return d ? `${d}.${m}.${y.slice(2)}` : this.getLabelForValue(i);
            },
          },
        },
        y: {
          grid: { color: "#ffffff0d" },
          title: yLabel ? { display: true, text: yLabel } : undefined,
          ticks: yTick ? { callback: yTick } : undefined,
        },
      },
      plugins: {
        legend: { display: datasets.length > 1, labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true } },
        tooltip: { backgroundColor: "#212837", borderColor: "#2c3446", borderWidth: 1, padding: 10 },
      },
    },
  });
}

// ---------------- rail / views ----------------
document.querySelectorAll("#rail nav button").forEach((b) => b.onclick = () => {
  document.querySelectorAll("#rail nav button, .view").forEach((e) => e.classList.remove("active"));
  b.classList.add("active");
  $(b.dataset.view).classList.add("active");
  // Telefonda ⋯ sayfasi (guncelle/cikis) ve filtre sayfasi acik kalmasin:
  // baska bir sekmeye gecen kullanici onlari kapatmis sayilir.
  closeSheets();
  if (b.dataset.view === "portfolio") loadPortfolio();
  if (b.dataset.view === "saved") renderSaved();
  if (b.dataset.view === "notify") loadNotifications();
  Object.values(charts).forEach((c) => c.resize());
});

const closeSheets = () => { $("more-t").checked = false; $("filt-t").checked = false; };
// Filtre sayfasi acilirken de ⋯ kapansin; ikisi ust uste binmesin.
$("filt-t").onchange = (e) => { if (e.target.checked) $("more-t").checked = false; };

// ---------------- status ----------------
let statusTimer;
async function loadStatus() {
  clearTimeout(statusTimer);
  try {
    const s = await api("/status");
    const tail = s.refresh.running
      ? `<b>güncelleniyor…</b><br>${esc(s.refresh.log.at(-1) || "")}`
      : s.refresh.error ? `<b class="down">hata:</b> ${esc(s.refresh.error)}` : "";
    $("status").innerHTML =
      `<b>${int(s.funds)}</b> fon · <b>${int(s.kap)}</b> KAP<br>son veri <b>${s.last_date || "yok"}</b><br>${tail}`;
    if (s.refresh.running) statusTimer = setTimeout(loadStatus, 3000);
    return s;
  } catch { $("status").textContent = "API'ye ulaşılamıyor."; }
}
$("refresh").onclick = async () => {
  const empty = !funds.length;
  try {
    await api("/refresh" + (empty ? "?days=90" : ""), { method: "POST" });
    loadStatus();
  } catch (e) { alert(e.message); }
};

// ---------------- scan ----------------
let funds = [];               // also the source for the compare picker
let sort = { k: "return_pct", dir: "desc" };

function applyPeriod(months) {
  $("s-end").value = lastDataDate;
  $("s-start").value = monthsBack(lastDataDate, months);
}
$("periods").onclick = (e) => {
  const b = e.target.closest("button[data-months]");
  if (!b) return;
  $("periods").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
  applyPeriod(+b.dataset.months);
  loadFunds();
};
// A hand-typed date is a different period than the preset says it is.
["s-start", "s-end"].forEach((id) => $(id).onchange = () =>
  $("periods").querySelectorAll("button").forEach((x) => x.classList.remove("on")));

document.querySelectorAll("#s-table th.sortable").forEach((th) => th.onclick = () => {
  sort = { k: th.dataset.k, dir: sort.k === th.dataset.k && sort.dir === "desc" ? "asc" : "desc" };
  document.querySelectorAll("#s-table th").forEach((x) => x.removeAttribute("data-dir"));
  th.dataset.dir = sort.dir;
  renderFunds();
});

// Leveraged funds report a negative money-market leg (e.g. +123.7 stock / -23.7
// BIST money market). Widths are scaled by the positive total so the bar still
// reads as a composition; the short leg is named in the tooltip instead.
function allocBar(groups) {
  if (!groups) return "";
  const longs = GROUPS.map((g, i) => [g, groups[g] || 0, i + 1]).filter(([, v]) => v > 0.5);
  const total = longs.reduce((s, [, v]) => s + v, 0);
  if (!total) return "";
  const shorts = Object.entries(groups).filter(([, v]) => v < 0);
  const bar = longs.map(([g, v, n]) =>
    `<i class="g${n}" style="width:${(v / total) * 100}%" title="${g} %${num(v, 1)}"></i>`).join("");
  const tip = shorts.length
    ? ` title="Kaldıraçlı: ${shorts.map(([g, v]) => `${g} %${num(v, 1)}`).join(", ")}"` : "";
  return `<div class="alloc"${tip}>${bar}</div>`;
}

function fundRow(f) {
  return `<tr class="clickable" data-code="${f.fund_code}">
    <td class="star-cell"><button class="star ${watched.has(f.fund_code) ? "on" : ""}"
      data-star="${f.fund_code}" title="${watched.has(f.fund_code) ? "Kaydı kaldır" : "Kaydet"}"
      aria-pressed="${watched.has(f.fund_code)}">${watched.has(f.fund_code) ? "★" : "☆"}</button></td>
    <td class="l"><span class="kod">${f.fund_code}</span></td>
    <td class="l name" title="${esc(f.fund_name)}">${esc(f.fund_name)}</td>
    <td class="l faint" style="font-size:12px;white-space:nowrap"
      title="${esc(f.unvan || "")}">${esc(f.unvan) || "—"}</td>
    <td class="l">${allocBar(f.groups)}</td>
    <td class="num">${num(f.last_price, 6)}</td>
    ${cell(f.daily_pct)}
    ${cell(f.return_pct)}
    <td class="num muted">${compact(f.portfolio_size)}</td>
    <td class="num muted">${int(f.investor_count)}</td></tr>`;
}

// 2.469 satırın hepsini DOM'a basmak her filtre/sıralama değişiminde ~4 sn
// sürüyordu. Sıralama tüm kümeye uygulanıp yalnızca baştaki dilim çiziliyor.
const PAGE = 250;
let shown = PAGE;

function renderFunds() {
  const { k, dir } = sort, s = dir === "asc" ? 1 : -1;
  const rows = [...funds].sort((a, b) => {
    const x = a[k], y = b[k];
    if (x == null) return 1;
    if (y == null) return -1;
    return (typeof x === "string" ? x.localeCompare(y, "tr") : x - y) * s;
  });
  $("s-rows").innerHTML = rows.slice(0, shown).map(fundRow).join("");
  $("s-empty").hidden = rows.length > 0;
  $("s-more").hidden = rows.length <= shown;
  $("s-more").textContent =
    `${int(rows.length)} fondan ilk ${int(shown)} tanesi gösteriliyor — devamını yükle`;
}

$("s-more").onclick = () => { shown += PAGE * 4; renderFunds(); };

const FILTERS = [["kind", "s-kind"], ["fon_turu", "s-turu"], ["unvan", "s-unvan"],
                 ["category", "s-cat"],
                 ["min_return", "s-min"], ["min_size", "s-size"]];
// Arama her tusta kendiliginden kosuyor; diger filtreler "Filtrele"ye basilana
// kadar uygulanmiyor, yoksa yarim birakilmis bir filtre aramayla birlikte
// istemeden devreye girerdi. `applied` en son onaylanan filtre kumesi.
let applied = {};

async function loadFunds({ onlyQuery = false } = {}) {
  if (!onlyQuery) {
    $("filt-t").checked = false;  // telefondaki filtre sayfasi acik kaldiysa kapansin
    applied = Object.fromEntries(
      FILTERS.filter(([, el]) => $(el).value).map(([key, el]) => [key, $(el).value]));
  }
  const p = new URLSearchParams({ start: $("s-start").value, end: $("s-end").value, ...applied });
  if ($("s-q").value) p.set("q", $("s-q").value);
  $("s-empty").hidden = false;
  $("s-empty").textContent = "Yükleniyor…";
  try {
    const d = await busy(api("/funds?" + p));
    funds = d.funds;
    shown = PAGE;  // yeni filtre, sayfalama başa dönsün
    // Eski bir surum bu listeleri gondermiyorsa secenekler bos kalir; tablonun
    // tamami hata verecegine o filtre calismasin.
    for (const [id, list] of [["s-cat", d.categories], ["s-unvan", d.unvanlar],
                              ["s-turu", d.turler]]) {
      if ($(id).options.length === 1 && list) {
        $(id).insertAdjacentHTML("beforeend",
          list.map((c) => `<option>${esc(c)}</option>`).join(""));
      }
    }
    renderFunds();
    $("s-empty").textContent = "Bu filtrelere uyan fon yok.";
  } catch (e) {
    $("s-rows").innerHTML = "";
    $("s-empty").innerHTML = `<span class="down">${esc(e.message)}</span>`;
  }
}
$("s-go").onclick = () => loadFunds();

// Yazarken ara: her tusa istek atmamak icin bekleyip son halini gonderir.
// Esik SADECE yazarken gecerli: tek harf heniz yazilmayi surduren bir kelime,
// onun icin istek atmak bosa. Enter ve Filtrele ise kullanicinin acik eylemi --
// ikisi de yazilani oldugu gibi arar, yoksa "Filtrele'ye bastim, bir sey olmadi"
// ya da tusla dugmenin farkli davrandigi bir arayuz cikiyor.
// Alan tamamen bosaltilinca filtresiz listeye donmek icin istek yine gidiyor.
const Q_MIN = 2;
const Q_DEBOUNCE = 500;
let qTimer;
$("s-q").oninput = () => {
  clearTimeout(qTimer);
  const v = $("s-q").value.trim();
  if (v && v.length < Q_MIN) return;
  qTimer = setTimeout(() => loadFunds({ onlyQuery: true }), Q_DEBOUNCE);
};
$("s-q").onkeydown = (e) => {
  if (e.key !== "Enter") return;
  clearTimeout(qTimer);
  loadFunds({ onlyQuery: true });
};

// ---------------- watchlist ----------------
let watched = new Set();

async function loadWatchlist() {
  try { watched = new Set((await api("/watchlist")).codes); } catch { /* boş kalsın */ }
  $("w-count").textContent = `${watched.size} fon`;
}

async function toggleWatch(code) {
  const on = watched.has(code);
  try {
    await api(`/watchlist/${code}`, { method: on ? "DELETE" : "PUT" });
    on ? watched.delete(code) : watched.add(code);
  } catch (e) { alert(e.message); return; }
  $("w-count").textContent = `${watched.size} fon`;
  // Patch the buttons in place: re-rendering the whole table would drop the
  // user's scroll position and keyboard focus mid-click.
  const now = watched.has(code);
  document.querySelectorAll(`button[data-star="${code}"]`).forEach((b) => {
    b.classList.toggle("on", now);
    b.textContent = now ? "★" : "☆";
    b.title = now ? "Kaydı kaldır" : "Kaydet";
    b.setAttribute("aria-pressed", now);
  });
  if ($("saved").classList.contains("active")) renderSaved();
}

// Row clicks open the drawer; the star must not.
function rowClick(e) {
  const star = e.target.closest("button[data-star]");
  if (star) { e.stopPropagation(); toggleWatch(star.dataset.star); return; }
  const tr = e.target.closest("tr[data-code]");
  if (tr) openDrawer(tr.dataset.code);
}
$("s-rows").onclick = rowClick;
$("w-rows").onclick = rowClick;

// Kaydedilenler kendi isteğini atar. Taramanın filtrelenmiş sonucunu süzmek,
// filtre veya dönem değiştiğinde kaydedilen fonun listeden düşmesine yol açıyordu.
async function renderSaved() {
  if (!watched.size) {
    $("w-rows").innerHTML = "";
    $("w-empty").hidden = false;
    $("w-empty").textContent = "Henüz fon kaydetmediniz. “Fonlar” listesinde ☆ işaretine basın.";
    return;
  }
  try {
    const p = new URLSearchParams({ start: $("s-start").value, end: $("s-end").value,
                                    codes: [...watched].join(",") });
    const d = await api("/funds?" + p);
    $("w-rows").innerHTML = d.funds.map(fundRow).join("");
    const missing = watched.size - d.funds.length;
    $("w-empty").hidden = d.funds.length > 0 && !missing;
    $("w-empty").textContent = d.funds.length
      ? `${missing} kaydedilen fonun bu tarih aralığında fiyat verisi yok.`
      : "Kaydedilen fonların bu tarih aralığında fiyat verisi yok.";
  } catch (e) {
    $("w-rows").innerHTML = "";
    $("w-empty").hidden = false;
    $("w-empty").innerHTML = `<span class="down">${esc(e.message)}</span>`;
  }
}

$("w-compare").onclick = () => {
  picked = [...watched].slice(0, 10);
  document.querySelector('#rail nav button[data-view="compare"]').click();
  drawCompare();
};

// ---------------- detail drawer ----------------
function closeDrawer() {
  $("drawer").classList.remove("open");
  $("scrim").classList.remove("open");
  $("drawer").setAttribute("aria-hidden", "true");
}
$("d-close").onclick = closeDrawer;
$("scrim").onclick = closeDrawer;
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("picker").hidden) $("picker").hidden = true;
  else closeDrawer();
});
document.querySelectorAll(".tabs button").forEach((b) => b.onclick = () => {
  document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("on", x === b));
  document.querySelectorAll(".pane").forEach((p) => p.classList.toggle("on", p.id === b.dataset.pane));
  Object.values(charts).forEach((c) => c.resize());
});

async function openDrawer(code) {
  $("drawer").classList.add("open");
  $("scrim").classList.add("open");
  $("drawer").setAttribute("aria-hidden", "false");
  $("d-kod").textContent = code;
  $("d-name").textContent = "Yükleniyor…";
  document.querySelector('.tabs button[data-pane="d-overview"]').click();

  let d;
  try {
    d = await api(`/funds/${code}?start=${$("s-start").value}&end=${$("s-end").value}`);
  } catch (e) { $("d-name").innerHTML = `<span class="down">${esc(e.message)}</span>`; return; }

  $("d-name").textContent = d.fund_name;
  $("d-cat").textContent = [d.kind, d.unvan, d.category].filter(Boolean).join(" · ");
  // Gunluk degisim fiyatin yanina: 5. kutu izgarada bos hucre birakiyordu,
  // "Sinifindaki payi"nda zaten kullanilan ikincil deger kalibi burada da isliyor.
  $("d-price").innerHTML = `${num2(d.price)}<i class="${cls(d.daily_pct)}">${pct(d.daily_pct)}</i>`;
  $("d-vol").textContent = d.volatility_pct == null ? "—" : num(d.volatility_pct) + "%";
  for (const [id, v] of [["d-ret", d.return_pct], ["d-mdd", d.max_drawdown_pct]]) {
    $(id).textContent = pct(v);
    $(id).className = cls(v);
  }
  $("d-size").textContent = d.portfolio_size ? "₺" + compact(d.portfolio_size) : "—";
  $("d-inv").textContent = int(d.investor_count);
  $("d-share").innerHTML = d.peer
    ? `${num(d.peer.share_pct)}%<i>${d.peer.rank}/${d.peer.count}</i>` : "—";
  $("d-periods").innerHTML = Object.entries(d.periods).map(([k, v]) =>
    `<div><small>${k} getiri</small><b class="${cls(v)}">${v == null ? "—" : pct(v)}</b></div>`).join("");
  line("d-chart", d.series.map((r) => r.date), [{ label: d.fund_code, data: d.series.map((r) => r.price) }], "₺");

  $("d-addpos").open = false;
  $("d-pmsg").textContent = "";
  $("d-punits").value = "";
  $("d-pdate").value = d.date;
  $("d-pprice").value = d.price;
  showPosSummary(code);

  loadForm(code);
  flowData = flowSeries(d.series);
  renderFlow($("d-flowseg").querySelector("button.on").dataset.flow);

  // allocation
  const groups = Object.entries(d.groups).sort((a, b) => b[1] - a[1]);
  $("d-allocdate").textContent = d.allocation_date
    ? `TEFAS dağılımı · ${d.allocation_date}` : "Dağılım verisi yok.";
  charts["d-donut"]?.destroy();
  const slices = groups.filter(([, v]) => v > 0);  // a short leg has no slice
  if (slices.length) {
    charts["d-donut"] = new Chart($("d-donut"), {
      type: "doughnut",
      data: {
        labels: slices.map(([g]) => g),
        datasets: [{
          data: slices.map(([, v]) => v),
          backgroundColor: slices.map(([g]) => GROUP_HEX[GROUPS.indexOf(g)] || "#5c6480"),
          borderColor: "#191e29", borderWidth: 2,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false, cutout: "62%",
        plugins: { legend: { display: false } },
      },
    });
  }
  $("d-legend").innerHTML = groups.map(([g, v]) =>
    `<div><em style="background:${GROUP_HEX[GROUPS.indexOf(g)] || "#5c6480"}"></em>
     <span>${g}</span><span class="${v < 0 ? "down" : ""}">${num(v)}%</span></div>`).join("")
    + (groups.some(([, v]) => v < 0)
       ? '<p class="faint" style="font-size:12px;margin:10px 0 0">Negatif kalem, fonun kaldıraç '
         + 'için borçlandığı tutarı gösterir; toplam yine %100’dür.</p>' : "");
  // The allocation pane is hidden while its chart is built, so Chart.js sizes it
  // to 0 and draws nothing until it is measured again.
  Object.values(charts).forEach((c) => c.resize());

  loadKap(code);
  loadHoldings(code, d.allocation);
}

// ---------------- yatirimci bilgi formu (KAP PDF) ----------------
// TEFAS stopaj/ucret/valor vermiyor; formun PDF'i sunucuda ayristiriliyor.
// Eslesmeyen alan bos kalir -- yanlis bir vergi orani gostermek daha kotu.
async function loadForm(code) {
  const cells = { stopaj_pct: "d-stopaj", management_fee_pct: "d-fee", valor_days: "d-valor" };
  Object.values(cells).forEach((id) => $(id).textContent = "…");
  let f;
  try { f = await api(`/funds/${code}/form`); }
  catch { Object.values(cells).forEach((id) => $(id).textContent = "—"); return; }
  $("d-stopaj").textContent = f.stopaj_pct == null ? "—" : `%${num2(f.stopaj_pct)}`;
  $("d-fee").textContent = f.management_fee_pct == null ? "—" : `%${num2(f.management_fee_pct)}`;
  $("d-valor").textContent = f.valor_days == null ? "—" : `${num2(f.valor_days)} gün`;
}

// ---------------- nakit akisi ----------------
// TEFAS giris/cikis tutarini vermiyor; tek turetilebilir yol pay sayisindaki
// gunluk degisimi o gunun fiyatiyla carpmak. Sonuc net giris (+) / cikis (-).
let flowData = [];

function flowSeries(series) {
  const out = [];
  for (let i = 1; i < series.length; i++) {
    const a = series[i - 1], b = series[i];
    if (a.shares_outstanding == null || b.shares_outstanding == null) continue;
    out.push({ date: b.date, flow: (b.shares_outstanding - a.shares_outstanding) * b.price });
  }
  return out;
}

function renderFlow(mode) {
  if (!flowData.length) {
    charts["d-flow"]?.destroy();
    delete charts["d-flow"];
    $("d-flownote").textContent = "Pay sayısı verisi yok, nakit akışı hesaplanamıyor.";
    return;
  }
  let acc = 0;
  const data = flowData.map((r) => (mode === "cum" ? (acc += r.flow) : r.flow));
  line("d-flow", flowData.map((r) => r.date), [{
    type: "bar", label: "Net akış", data, borderWidth: 0,
    backgroundColor: data.map((v) => (v >= 0 ? "#35c88a" : "#e5626b")),
  }], "₺", compact);
  const total = flowData.reduce((t, r) => t + r.flow, 0);
  $("d-flownote").textContent = `Seçili dönemde net ${total >= 0 ? "giriş" : "çıkış"}: `
    + `₺${compact(Math.abs(total))} · yeşil giriş, kırmızı çıkış.`;
}

$("d-flowseg").onclick = (e) => {
  const b = e.target.closest("button[data-flow]");
  if (!b) return;
  $("d-flowseg").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
  renderFlow(b.dataset.flow);
};

// ---------------- item-level holdings (KAP PDF) ----------------
// Keyed by the TEFAS allocation column, so an allocation row can open its own
// constituents. Only sections whose extracted total agrees with TEFAS are
// offered: the PDF's own section names do not map cleanly onto TEFAS categories.
let drill = { code: null, bySection: {}, previous: null, keyToSection: {} };

function drillable(key) {
  const s = drill.bySection[drill.keyToSection[key]];
  return s && s.items.length ? s : null;
}

function subTable(s) {
  const cmp = drill.previous;
  const head = `<tr><th class="l">Kalem</th><th class="l">Tanım</th><th>Değer ₺</th>
    <th>Ağırlık %</th>${cmp ? "<th>Önceki %</th><th>Değişim</th>" : ""}</tr>`;
  const body = s.items.map((h) => {
    const badge = h.status === "yeni" ? '<span class="badge new">yeni</span>'
      : h.status === "cikti" ? '<span class="badge out">çıktı</span>' : "";
    return `<tr>
      <td class="l"><span class="kod">${esc(h.code)}</span> ${badge}</td>
      <td class="l name faint" title="${esc(h.issuer || h.isin)}"
        >${esc(h.issuer) || `<span class="num">${esc(h.isin) || "—"}</span>`}</td>
      <td class="num muted">${h.value ? compact(h.value) : "—"}</td>
      <td class="num">${h.status === "cikti" ? "—" : num(h.weight_pct)}</td>
      ${cmp ? `<td class="num muted">${h.prev_weight_pct == null ? "—" : num(h.prev_weight_pct)}</td>
               <td class="num ${cls(h.delta)}">${h.delta == null ? "—" : pct(h.delta)}</td>` : ""}
    </tr>`;
  }).join("");
  // Sayilar tutmadiginda kalemleri gizlemek yerine farki yaziyoruz: KAP raporu
  // ay sonu, TEFAS dagilimi bugun -- aradaki oynama tek basina veriyi curutmuyor.
  const note = s.match === "tam" ? "" :
    `<p class="faint" style="font-size:12px;margin:8px 0 0">Rapordan
       %${num(s.extracted_pct)} okundu, TEFAS %${num(s.tefas_pct ?? 0)} bildiriyor.
       ${drill.current ? `KAP raporu ${drill.current} tarihli` : ""} — fark, fonun o
       tarihten beri değişmiş olmasından ya da rapor başlığının TEFAS kategorisiyle
       birebir örtüşmemesinden kaynaklanabilir.</p>`;
  return `<div class="subwrap"><table><thead>${head}</thead><tbody>${body}</tbody></table>${note}</div>`;
}

function renderAllocRows(allocation) {
  const entries = Object.entries(allocation).sort((a, b) => b[1] - a[1]);
  $("d-rows").innerHTML = entries.map(([k, v]) => {
    const s = drillable(k);
    return `<tr class="${s ? "drill" : "plain"}" data-key="${k}">
      <td class="l">${allocLabel(k)}${s ? `<span class="faint" style="font-size:11px">
        · ${s.items.length} kalem</span>` : ""}</td>
      <td class="num ${v < 0 ? "down" : ""}">${num(v)}</td></tr>`;
  }).join("") || '<tr><td colspan="2" class="faint l">Kalem bazlı dağılım yok.</td></tr>';
}

$("d-rows").onclick = (e) => {
  const tr = e.target.closest("tr.drill");
  if (!tr) return;
  const open = tr.nextElementSibling?.classList.contains("sub");
  tr.parentElement.querySelectorAll("tr.sub").forEach((x) => x.remove());
  tr.parentElement.querySelectorAll("tr.drill").forEach((x) => x.classList.remove("open"));
  if (open) return;
  tr.classList.add("open");
  tr.insertAdjacentHTML("afterend",
    `<tr class="sub"><td colspan="2">${subTable(drillable(tr.dataset.key))}</td></tr>`);
};

async function loadHoldings(code, allocation) {
  drill = { code, bySection: {}, previous: null, keyToSection: {} };
  renderAllocRows(allocation);
  let d;
  try { d = await api(`/funds/${code}/holdings`); }
  catch { return; }

  drill = { code, bySection: d.sections, previous: d.previous_report,
            current: d.current_report, keyToSection: d.key_to_section };
  renderAllocRows(allocation);

  const extractable = Object.keys(d.tefas_sections).length > 0;
  if (d.reports.length) {
    const n = Object.values(d.sections).reduce((t, s) => t + s.items.length, 0);
    $("d-extractbar").innerHTML = `<p class="faint num" style="font-size:12px;margin:0">
      KAP raporu ${d.current_report} · ${n} kalem${d.previous_report
        ? ` · ${d.previous_report} ile kıyaslanıyor` : " · kıyas için ikinci rapor yok"}
      </p><p class="faint" style="font-size:12px;margin:6px 0 0">
      Yanında ▸ olan satırlara tıklayıp kalemleri görebilirsiniz.</p>`;
    return;
  }
  if (!extractable) { $("d-extractbar").innerHTML = ""; return; }
  $("d-extractbar").innerHTML = `<div class="note">Bu varlıkların <b>hangi kalemler</b>
    olduğu TEFAS'ta yok; yalnızca KAP'ın aylık portföy dağılım raporunda bulunuyor.</div>
    <button class="btn primary" id="d-extract">KAP raporundan çıkar</button>
    <span class="faint" id="d-exmsg" style="margin-left:10px"></span>`;
  $("d-extract").onclick = async () => {
    $("d-extract").disabled = true;
    $("d-exmsg").textContent = "Son iki rapor indiriliyor ve ayrıştırılıyor… (20-40 sn)";
    try {
      await api(`/funds/${code}/holdings`, { method: "POST" });
      loadHoldings(code, allocation);
    } catch (e) {
      $("d-exmsg").innerHTML = `<span class="down">${esc(e.message)}</span>`;
      $("d-extract").disabled = false;
      $("d-extract").textContent = "Tekrar dene";
    }
  };
}

async function loadKap(code) {
  $("d-kaplist").innerHTML = '<p class="faint">Yükleniyor…</p>';
  try {
    const { disclosures } = await api(`/funds/${code}/kap`);
    if (!disclosures.length) {
      $("d-kaplist").innerHTML =
        '<p class="faint">Bu fona ait, önbellekteki dönemde KAP bildirimi yok.</p>';
      return;
    }
    $("d-kaplist").innerHTML = disclosures.map((x) => {
      const isPortfolio = (x.subject || "").startsWith("Portföy Dağılım");
      return `<div class="discl">
        <a href="${x.url}" target="_blank" rel="noopener noreferrer">${esc(x.subject || "Bildirim")}</a>
        ${isPortfolio ? '<span class="tag">portföy detayı</span>' : ""}
        <p>${x.publish_date.slice(0, 16)}${x.summary ? " · " + esc(x.summary.trim().slice(0, 120)) : ""}</p>
        ${x.attachment_count ? `<p><button class="btn ghost" data-att="${x.disclosure_index}"
           style="padding:3px 8px;font-size:12px">Ek dosyalar (${x.attachment_count})</button></p>` : ""}
      </div>`;
    }).join("");
  } catch (e) { $("d-kaplist").innerHTML = `<p class="down">${esc(e.message)}</p>`; }
}

$("d-kaplist").onclick = async (e) => {
  const f = e.target.closest("button[data-file]");
  if (f) {
    f.disabled = true;
    try {
      const url = URL.createObjectURL(await api(`/kap/file/${f.dataset.file}`, { blob: true }));
      Object.assign(document.createElement("a"), { href: url, download: f.dataset.name }).click();
      // Hemen iptal etmek indirmeyi yarida kesebiliyor.
      setTimeout(() => URL.revokeObjectURL(url), 30000);
    } catch (err) {
      f.insertAdjacentHTML("afterend",
        `<span class="down" style="font-size:12px"> ${esc(err.message)}</span>`);
    }
    f.disabled = false;
    return;
  }
  const b = e.target.closest("button[data-att]");
  if (!b) return;
  b.disabled = true;
  b.textContent = "Yükleniyor…";
  try {
    const { attachments } = await api(`/kap/${b.dataset.att}/attachments`);
    b.outerHTML = attachments.length
      ? attachments.map((a) => `<button class="btn ghost" data-file="${esc(a.obj_id)}"
          data-name="${esc(a.file_name)}" style="padding:3px 8px;font-size:12px;display:block"
          >↓ ${esc(a.file_name)}</button>`).join("")
      : '<span class="faint" style="font-size:12px">Ek bulunamadı.</span>';
  } catch (err) {
    b.disabled = false;
    b.textContent = "Tekrar dene";
    b.insertAdjacentHTML("afterend", `<span class="down" style="font-size:12px"> ${esc(err.message)}</span>`);
  }
};

// ---------------- KAP bildirimleri ----------------
// KAP'in push/websocket ucu yok: sunucu her saatin 5'inde KAP'i yokluyor, burasi
// da o onbellegi okuyor. Okundu isareti cihazda -- gorulen en buyuk bildirim
// numarasi yetiyor, sunucuda kullanici basina tablo acmaya degmez.
// Temizleme de ayni sekilde cihazda: kap_disclosures onbellegi tum kullanicilar
// icin ortak, sunucudan silmek baskasinin bildirimini de gotururdu.
const localMark = (key) => ({
  get: () => +localStorage.getItem(key) || 0,
  set: (n) => localStorage.setItem(key, n),
});
const seen = localMark("fonlu-seen");
const cleared = localMark("fonlu-cleared");
let notices = [];

const lastIndex = () => Math.max(...notices.map((x) => x.disclosure_index));

function renderNotifications() {
  const readMark = seen.get();
  const shownNotices = notices.filter((x) => x.disclosure_index > cleared.get());
  $("n-list").innerHTML = shownNotices.map((x) => `<div class="discl">
    <a href="${x.url}" target="_blank" rel="noopener noreferrer">${esc(x.subject || "Bildirim")}</a>
    ${x.disclosure_index > readMark ? '<span class="tag">yeni</span>' : ""}
    <p><span class="kod">${x.fund_code}</span> · ${x.publish_date.slice(0, 16)}${
      x.summary ? " · " + esc(x.summary.trim().slice(0, 140)) : ""}</p>
  </div>`).join("");
  $("n-empty").hidden = shownNotices.length > 0;
  $("n-empty").textContent = notices.length
    ? "Bildirimler temizlendi; yenisi geldiğinde burada görünür."
    : "Takip ettiğin ya da portföyündeki fonlar için bildirim yok.";
  const fresh = shownNotices.filter((x) => x.disclosure_index > readMark).length;
  $("n-badge").hidden = !fresh;
  $("n-badge").textContent = fresh > 99 ? "99+" : fresh;
}

async function loadNotifications() {
  try { notices = (await api("/notifications")).disclosures; }
  catch (e) {
    $("n-list").innerHTML = "";
    $("n-empty").hidden = false;
    $("n-empty").innerHTML = `<span class="down">${esc(e.message)}</span>`;
    return;
  }
  renderNotifications();
}

$("n-seen").onclick = () => {
  if (notices.length) seen.set(lastIndex());
  renderNotifications();
};

$("n-clear").onclick = () => {
  if (!notices.length) return;
  // Temizlemek okumak demek: aksi halde liste bosalir ama rozet yanik kalir.
  cleared.set(lastIndex());
  seen.set(lastIndex());
  renderNotifications();
};

// ---------------- compare ----------------
let picked = [];

$("c-pick").onclick = async () => {
  if (!funds.length) await loadFunds();
  $("picker").hidden = false;
  $("pk-q").value = "";
  renderPicker();
  $("pk-q").focus();
};
$("pk-cancel").onclick = () => { $("picker").hidden = true; };
$("pk-q").oninput = renderPicker;

function renderPicker() {
  const q = $("pk-q").value.trim().toUpperCase();
  const hits = funds.filter((f) =>
    !q || f.fund_code.includes(q) || f.fund_name.toUpperCase().includes(q));
  $("pk-count").textContent = `${hits.length} fon`;
  // Cap the DOM: 2400 rows with checkboxes janks the panel open.
  $("pk-list").innerHTML = hits.slice(0, 300).map((f) => `<label>
    <input type="checkbox" value="${f.fund_code}" ${picked.includes(f.fund_code) ? "checked" : ""}>
    <span class="kod">${f.fund_code}</span>
    <span class="name" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
      >${esc(f.fund_name)}</span>
    <span class="num ${cls(f.return_pct)}">${pct(f.return_pct)}</span></label>`).join("")
    + (hits.length > 300 ? '<p class="faint" style="padding:12px 18px">İlk 300 sonuç gösteriliyor — aramayı daraltın.</p>' : "");
  updateSel();
}

$("pk-list").onchange = (e) => {
  const cb = e.target;
  if (cb.checked) {
    if (picked.length >= 10) { cb.checked = false; return; }
    picked.push(cb.value);
  } else picked = picked.filter((c) => c !== cb.value);
  updateSel();
};
const updateSel = () => { $("pk-sel").textContent = `${picked.length}/10 seçili`; };

$("pk-ok").onclick = () => { $("picker").hidden = true; drawCompare(); };
$("c-chips").onclick = (e) => {
  const b = e.target.closest("button[data-code]");
  if (!b) return;
  picked = picked.filter((c) => c !== b.dataset.code);
  drawCompare();
};
["c-start", "c-end"].forEach((id) => $(id).onchange = () => picked.length && drawCompare());

async function drawCompare() {
  $("c-chips").innerHTML = picked.map((c) =>
    `<button data-code="${c}" title="Çıkar">${c} ×</button>`).join("");
  if (!picked.length) {
    charts["c-chart"]?.destroy();
    delete charts["c-chart"];
    $("c-rows").innerHTML = "";
    $("c-empty").hidden = false;
    $("c-empty").textContent = "Karşılaştırmak için “Fon seç”e basın.";
    return;
  }
  $("c-empty").hidden = true;
  try {
    const d = await api(`/compare?codes=${picked.join(",")}` +
      `&start=${$("c-start").value}&end=${$("c-end").value}`);
    const dates = [...new Set(d.funds.flatMap((f) => f.series.map((p) => p.date)))].sort();
    line("c-chart", dates, d.funds.map((f) => {
      const by = Object.fromEntries(f.series.map((p) => [p.date, p.value]));
      return { label: f.fund_code, data: dates.map((dt) => by[dt] ?? null), spanGaps: true };
    }), "başlangıç = 100");
    const byCode = Object.fromEntries(funds.map((f) => [f.fund_code, f.fund_name]));
    $("c-rows").innerHTML = d.funds.map((f) => `<tr>
      <td class="l"><span class="kod">${f.fund_code}</span></td>
      <td class="l name">${esc(byCode[f.fund_code] || "")}</td>${cell(f.return_pct)}</tr>`).join("");
    if (d.missing.length) {
      $("c-empty").hidden = false;
      $("c-empty").innerHTML = `Bu dönemde veri yok: <b>${d.missing.join(", ")}</b>`;
    }
  } catch (e) {
    $("c-empty").hidden = false;
    $("c-empty").innerHTML = `<span class="down">${esc(e.message)}</span>`;
  }
}

// ---------------- portfolio ----------------
// Detay cekmecesinden pozisyon ekleme. Kod cekmecenin basligindan geliyor,
// not alani yok -- portfoy ekranindaki tam form onun icin duruyor.
$("d-posform").onsubmit = async (e) => {
  e.preventDefault();
  $("d-pmsg").textContent = "";
  try {
    await api("/positions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        fund_code: $("d-kod").textContent, units: +$("d-punits").value,
        buy_date: $("d-pdate").value, buy_price: +$("d-pprice").value,
      }),
    });
    $("d-pmsg").className = "up";
    $("d-pmsg").textContent = "Portföye eklendi.";
    $("d-punits").value = "";
    await loadPortfolio();
    showPosSummary($("d-kod").textContent);
  } catch (err) {
    $("d-pmsg").className = "down";
    $("d-pmsg").textContent = err.message;
  }
};

$("p-form").onsubmit = async (e) => {
  e.preventDefault();
  $("p-msg").textContent = "";
  try {
    await api("/positions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        fund_code: $("p-code").value, units: +$("p-units").value,
        buy_date: $("p-date").value, buy_price: +$("p-price").value,
        note: $("p-note").value || null,
      }),
    });
    e.target.reset();
    loadPortfolio();
  } catch (err) { $("p-msg").textContent = err.message; }
};

// Cekmecedeki ozet ayni veriden besleniyor; portfoy her yuklendiginde tazeleniyor.
let myPositions = null;

/** Fon portfoydeyse ekleme formunun ustunde tek satirlik ozet gosterir. */
function showPosSummary(code) {
  const mine = (myPositions || []).filter((p) => p.fund_code === code);
  $("d-possum").hidden = !mine.length;
  if (!mine.length) return;
  const sum = (f) => mine.reduce((a, p) => a + (f(p) || 0), 0);
  const units = sum((p) => p.units), cost = sum((p) => p.cost), value = sum((p) => p.value);
  // Deger ancak son fiyat varsa dolu; yoksa K/Z uydurmak yerine "—" kalsin.
  const has = mine.every((p) => p.value != null);
  const profit = has ? value - cost : null;
  $("d-possum").innerHTML = `
    <div><span>Portföyde</span><b>${num(units, 4)} adet</b></div>
    <div><span>Maliyet</span><b>${num(cost)} ₺</b></div>
    <div><span>Değer</span><b>${has ? num(value) + " ₺" : "—"}</b></div>
    <div><span>K/Z</span><b class="${cls(profit)}">${profit == null ? "—" : num(profit) + " ₺"}</b></div>
    <div><span>Getiri</span><b class="${cls(profit)}">${pct(cost ? (profit / cost) * 100 : null)}</b></div>`;
  $("d-addpos").open = true;
}

async function loadPortfolio() {
  const d = await api("/positions");
  myPositions = d.positions;
  $("p-kpi").innerHTML = `
    <div><small>Maliyet</small><b>${num(d.total_cost)} ₺</b></div>
    <div><small>Güncel değer</small><b>${num(d.total_value)} ₺</b></div>
    <div><small>Kar / zarar</small><b class="${cls(d.total_profit)}">${num(d.total_profit)} ₺</b></div>
    <div><small>Getiri</small><b class="${cls(d.total_profit_pct)}">${pct(d.total_profit_pct)}</b></div>`;
  $("p-rows").innerHTML = d.positions.map((p) => `<tr class="clickable" data-code="${p.fund_code}">
    <td class="l"><span class="kod">${p.fund_code}</span></td>
    <td class="l name" title="${esc(p.fund_name || "")}">${esc(p.fund_name || "—")}</td>
    <td class="num">${num(p.units, 4)}</td><td class="num">${num(p.buy_price, 6)}</td>
    <td class="num">${num(p.last_price, 6)}</td><td class="num">${num(p.cost)}</td>
    <td class="num">${num(p.value)}</td>
    <td class="num ${cls(p.profit)}">${num(p.profit)}</td>${cell(p.profit_pct)}
    <td><button class="btn ghost" data-del="${p.id}" style="padding:3px 8px;font-size:12px">Sil</button></td>
    </tr>`).join("");
  $("p-empty").hidden = d.positions.length > 0;
}
$("p-rows").onclick = async (e) => {
  const b = e.target.closest("button[data-del]");
  if (!b) return rowClick(e);  // silme disindaki her yer detayi acar
  if (!confirm("Pozisyon silinsin mi?")) return;
  await api(`/positions/${b.dataset.del}`, { method: "DELETE" });
  loadPortfolio();
};

// ---------------- init ----------------
async function boot() {
  // Dönem alanları piyasadaki son veri gününe göre kurulmalı; bunun için status
  // beklenir, yoksa 1A dönemi bugüne göre kurulup TEFAS'la kayıyor.
  // Tek /status yetiyor: hem rozeti dolduruyor hem son veri gununu veriyor.
  // Iki ayri cagri acilisa bos yere bir tur daha ekliyordu.
  await busy((async () => {
    lastDataDate = (await loadStatus())?.last_date || lastDataDate;
    applyPeriod(1);
    $("c-end").value = lastDataDate;
    $("c-start").value = monthsBack(lastDataDate, 3);
    await loadWatchlist();
    await loadFunds();
  })());
  // Rozet ve cekmecedeki portfoy ozeti icin; acilisi bekletmiyorlar.
  loadNotifications();
  loadPortfolio();
}

// ---------------- giriş kapısı ----------------
function showAuthGate() {
  $("auth-gate").hidden = false;
  $("app-shell").hidden = true;
}

function authError(msg) {
  const el = $("auth-error");
  el.textContent = msg;
  el.hidden = !msg;
}

async function start() {
  if (!session.get()) return showAuthGate();
  $("auth-gate").hidden = true;
  $("app-shell").hidden = false;
  await boot();   // uygulamanin mevcut acilis fonksiyonu
}

$("auth-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  authError("");
  try {
    await signIn($("auth-email").value.trim(), $("auth-pass").value);
    await start();
  } catch (err) { authError(err.message); }
});

$("auth-passkey").addEventListener("click", async () => {
  authError("");
  try {
    await passkeySignIn();
    await start();
  } catch (err) { authError(err.message); }
});

$("add-passkey").addEventListener("click", async () => {
  try {
    await passkeyRegister();
    alert("Passkey eklendi.");
  } catch (err) { alert(err.message); }
});

// ponytail: prompt() sifreyi ekranda acikta gosteriyor. Kendi cihazinda tek
// seferlik bir islem icin yeterli; rahatsiz ederse auth-gate'teki gibi bir
// <input type="password"> formuna cevrilir.
$("set-pass").addEventListener("click", async () => {
  const pass = prompt("Yeni şifre (en az 6 karakter):");
  if (!pass) return;
  if (pass.length < 6) return alert("Şifre en az 6 karakter olmalı.");
  try {
    await updatePassword(pass);
    alert("Şifre belirlendi. Bundan sonra e-posta ve şifreyle de girebilirsiniz.");
  } catch (err) { alert(err.message); }
});

$("logout").addEventListener("click", () => { session.clear(); location.reload(); });

// Davet ve sifre sifirlama linkleri sonucu URL fragment'inda getiriyor:
// ya #access_token=... ya da #error=... . Fragment'i her halukarda adres
// cubugundan siliyoruz; token URL'de kalirsa tarayici gecmisine yaziliyor ve
// link paylasilirsa oturum da gidiyor.
const hash = new URLSearchParams(location.hash.slice(1));
if (location.hash.length > 1) {
  history.replaceState(null, "", location.pathname + location.search);
}

const hashToken = hash.get("access_token");
if (hashToken) {
  session.set({ access_token: hashToken, refresh_token: hash.get("refresh_token") });
}

start().then(() => {
  // Davet token'i tek kullanimlik: link ikinci kez acilirsa (ya da suresi
  // dolduysa) buraya hata ile donuyor. Sessizce yutulursa kullanici sebepsiz
  // bir giris ekraninda kaliyor.
  const err = hash.get("error_description") || hash.get("error");
  if (err && !hashToken) authError(`Davet linki gecersiz: ${err}`);
});
