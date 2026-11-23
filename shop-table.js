// Таблиця «Замовлення по днях»: дані, правила магазинів і малювання.
//
// Один файл на дві сторінки — головну й публічне посилання /shop/<ключ>.
// Сторінка-господар кладе дані у CRM_RAW і RING_RAW та викликає
// renderShop(); більше від неї нічого не потрібно.

// ── дрібні помічники ──
const fmt=n=>new Intl.NumberFormat('uk-UA').format(Math.round(n||0));
const fmt1=n=>new Intl.NumberFormat('uk-UA',{minimumFractionDigits:1,maximumFractionDigits:1}).format(n||0);
function kpiIso(d){
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}
function ageMin(stamp){
  if(!stamp) return Infinity;
  const t = Date.parse(String(stamp).replace(' ', 'T'));   // місцевий час сервера
  return isNaN(t) ? Infinity : (Date.now() - t) / 60000;
}
function fmtAge(min){
  if(!isFinite(min)) return '';
  if(min < 1) return 'щойно';
  if(min < 60) return `${Math.round(min)} хв тому`;
  const h=Math.floor(min/60);
  return h < 24 ? `${h} год тому` : `${Math.floor(h/24)} дн. тому`;
}
function escapeHtml(text){
  const map={'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'};
  return String(text==null?'':text).replace(/[&<>"']/g,m=>map[m]);
}

// ── щоденна таблиця інтернет-магазину ───────────────────────────────────
// Рахується прямо тут, із двох файлів, які сторінка й так завантажує:
// угоди з Bitrix і дзвінки з Ringostat. Окремий збирач і третій файл даних
// для цього не потрібні.
//
// Три канали замовлень і те, чим вони розрізняються:
//   Чатбот  — джерело угоди «Simpler» (FNB_SIMPLER)
//   Кошик   — джерело угоди «Заявка симплер» (UC_8AMYMZ)
//   Дзвінки — угода, чий телефон клієнта збігся з дзвінком ІЗ ЦЬОГО САЙТУ
//             того ж дня. Джерело угоди при цьому будь-яке: у Bitrix немає
//             поля «з якого сайту дзвінок», воно є тільки в Ringostat.
// Телефони ніде не зберігаються — звіряються хеші (див. phone_key.py).
// Вкладки таблиці. У кожної свій набір колонок, бо питання різні: на
// «Усіх магазинах» цікаво, ЯКИМ КАНАЛОМ прийшло замовлення, а на вкладці
// магазину — скільки цей магазин дав.
//
// Як колонка ловить угоду:
//   sources  — джерело угоди (поле «Джерело» в Bitrix);
//   site     — мітка сайту з поля «Додатково про джерело» (у matroluxe.com
//              і matroluxe.ua джерело однакове, «WEB», і відрізняє їх лише
//              вона; розбирає її fetch_bitrix.py);
//   callSite — телефон клієнта збігся з дзвінком ІЗ ЦЬОГО САЙТУ того ж дня.
//              Потрібно тому, що в Bitrix немає поля «з якого сайту дзвінок»,
//              воно є тільки в Ringostat. Такі колонки перевіряються
//              ОСТАННІМИ, інакше вони крали б угоди в кошика.
//
// Старі канали злиті з новими, які їх замінили: Jivosite — це ручні угоди
// з чату до появи ШІ-чатів, Instagram і Facebook — ручні угоди до Meta.
// Інакше історія рвалася б надвоє на даті переходу.
const CH_BOT   = ['FNB_SIMPLER', 'FNB_MUA', 'FNB_MCOM', 'WEBFORM'];
const CH_CART  = ['UC_5B6PJK', 'UC_8AMYMZ', '7'];
const CH_FORM  = ['CALLBACK', 'WEB - Зворотний дзвінок', 'RC_GENERATOR',
                  'UC_HYAP6C', 'OTHER', ''];
const CH_ROZET = ['UC_S1T3W0', 'UC_9LCW2A'];
const CH_META  = ['FNB_META', '1', 'STORE'];

const SHOPS = [
  {
    key: 'all', name: 'Усі магазини',
    // Непізнане джерело не губимо, а показуємо окремою колонкою «Інше»:
    // мовчки з'їдений канал зробив би підсумок неправдивим, а новий
    // источник у Bitrix інакше ніхто б і не помітив.
    rest: true,
    cols: [
      {k: 'bot',   label: 'Чатбот',  sources: CH_BOT},
      {k: 'call',  label: 'Дзвінки', sources: ['CALL']},
      {k: 'cart',  label: 'Кошик',   sources: CH_CART},
      {k: 'form',  label: 'Форма',   sources: CH_FORM},
      {k: 'rozet', label: 'Розетка', sources: CH_ROZET},
      {k: 'meta',  label: 'Meta',    sources: CH_META},
    ],
  },
  {
    key: 'simpler', name: 'Simpler',
    cols: [
      {k: 'bot',  label: 'Чатбот',  sources: ['FNB_SIMPLER']},
      {k: 'call', label: 'Дзвінки', callSite: 'simplershop'},
      {k: 'cart', label: 'Кошик',   sources: ['UC_8AMYMZ']},
    ],
  },
  {
    key: 'com', name: 'Matroluxe COM',
    cols: [
      {k: 'bot',  label: 'Чатбот',  sources: ['FNB_MCOM']},
      {k: 'call', label: 'Дзвінки', callSite: 'matroluxe.com'},
      {k: 'cart', label: 'Кошик',   site: 'matroluxe.com'},
    ],
  },
  {
    key: 'ua', name: 'Matroluxe UA',
    cols: [
      {k: 'bot',  label: 'Чатбот',  sources: ['FNB_MUA']},
      {k: 'call', label: 'Дзвінки', callSite: 'matroluxe.ua'},
      {k: 'cart', label: 'Кошик',   site: 'matroluxe.ua'},
    ],
  },
];
// Товарні категорії. Назви ключів — ті самі, що пише fetch_bitrix.py у
// deal_categories. Аксесуари лежать окремим полем, а не всередині
// categories, тому в списку позначені прапорцем.
const CATS = [
  {k: 'mattresses',     label: 'Матраци'},
  {k: 'soft_furniture', label: 'М\'які меблі'},
  {k: 'case_furniture', label: 'Корпусні меблі'},
  {k: 'wardrobes',      label: 'Шафи'},
  {k: 'accessories',    label: 'Аксесуари', own: true},
];

// Колонки для лідів — ті самі канали, що й у «Усіх магазинах»: ліди
// приходять тими ж шляхами, що й замовлення.
const LEAD_COLS = SHOPS[0].cols;

const SHOP_KEY = 'shop-tab';
const VIEW_KEY = 'shop-view';
let shopView = 'channels';   // 'channels' | 'cats' | 'leads'
const SHOP_COLLAPSED = 5;   // скільки днів видно, поки не натиснули «+»
let shopIdx = 0;

const MONTHS_UA = ['січень','лютий','березень','квітень','травень','червень',
                   'липень','серпень','вересень','жовтень','листопад','грудень'];
// Назва місяця у родовому відмінку: «усі дні листопада», а не «листопад».
const MONTHS_UA_GEN = ['січня','лютого','березня','квітня','травня','червня',
                       'липня','серпня','вересня','жовтня','листопада','грудня'];
let shopMonth = null;    // 'РРРР-ММ'; null — поточний
let shopOpen  = false;   // чи розгорнули на весь місяць

function shopMatch(col, d){
  if(col.sources && col.sources.indexOf(d.source_id || '') >= 0) return true;
  if(col.site && (d.site || '') === col.site) return true;
  return false;
}

const REST_KEY = '__rest';

function shopRows(shop, crm, ring){
  const turn = {};
  (crm.deal_categories||[]).forEach(c=>{ turn[c.deal_id] = c.turnover || 0; });

  // Колонки за джерелом перевіряємо першими, за дзвінком — останніми.
  const direct = shop.cols.filter(c=>!c.callSite);
  const byCall = shop.cols.filter(c=>c.callSite);

  // Номери, що дзвонили з потрібного сайту, по днях.
  const callDays = {};
  let siteKnown = 0;
  if(byCall.length){
    (ring && ring.calls || []).forEach(c=>{
      if(c.site) siteKnown++;
      if(!c.date || !c.caller_key) return;
      byCall.forEach(col=>{
        if(c.site !== col.callSite) return;
        const m = callDays[col.k] = callDays[col.k] || {};
        (m[c.date] = m[c.date] || new Set()).add(c.caller_key);
      });
    });
  }

  const rows = {};
  const members = {};   // день → Set(id угод цієї вкладки)
  (crm.deals||[]).forEach(d=>{
    if(!d.date) return;
    let key = null;
    for(const col of direct){ if(shopMatch(col, d)){ key = col.k; break; } }
    if(!key){
      for(const col of byCall){
        const nums = (callDays[col.k] || {})[d.date];
        if(nums && (d.phone_keys||[]).some(k=>nums.has(k))){ key = col.k; break; }
      }
    }
    if(!key && shop.rest) key = REST_KEY;
    if(!key) return;
    const r = rows[d.date] = rows[d.date] || {};
    const cur = r[key] = r[key] || [0, 0];
    cur[0] += 1; cur[1] += turn[d.id] || 0;
    (members[d.date] = members[d.date] || new Set()).add(String(d.id));
  });
  return {rows, members, turn, siteKnown, hasCall: byCall.length > 0};
}

// Ліди по тих самих каналах, що й угоди.
//
// Чому окремо від угод: у ліда немає ні товарних рядків, ні суми (в Bitrix
// вона нульова), зате є статус — «Якісний лід», «Некваліфікований»,
// «Дублікат». Тому клітинка показує не гроші, а скільки лідів і скільки з
// них визнали якісними.
//
// Дублікати рахуються в «лідів», але виключені зі знаменника конверсії:
// один і той самий клієнт, що подзвонив двічі, не робить канал гіршим.
const LEAD_DUP = '1';          // статус «Дублікат»
const LEAD_GOOD = 'CONVERTED'; // статус «Якісний лід»

// Частка кожного магазину в загальному обороті.
//
// Одна угода рахується рівно в одному магазині: перший збіг виграє. Інакше
// клієнт, який того самого дня дзвонив на два сайти, потрапив би в обидва,
// і сума часток перевалила б за сто відсотків — а такій таблиці вже не
// вірять. Усе, що не впізнали, чесно лежить в «Інше»: мовчки викинути
// угоду означало б показати неправдиву частку решти.
const SHARE_COLORS = ['#32D8FF', '#FF8C42', '#9B8CFF', '#27E37A', '#95A0BF'];

// Чий це запис: індекс магазину в SHOPS без «усіх», або -1.
//
// Та сама логіка потрібна і часткам магазинів, і блокам за джерелом
// трафіку. Тримаємо її в одному місці: якщо правила розійдуться, та сама
// угода опиниться в різних магазинах у сусідніх блоках однієї сторінки,
// і довіри до жодного з них уже не буде.
function shopOwnerFn(crm, ring){
  const list = SHOPS.filter(s=>!s.rest);
  // Для кожного магазину — хто дзвонив з його сайту, по днях.
  const callDays = list.map(shop=>{
    const m = {};
    const cols = shop.cols.filter(c=>c.callSite);
    (ring && ring.calls || []).forEach(c=>{
      if(!c.date || !c.caller_key) return;
      if(!cols.some(col=>col.callSite === c.site)) return;
      (m[c.date] = m[c.date] || new Set()).add(c.caller_key);
    });
    return m;
  });
  return rec => {
    for(let i = 0; i < list.length; i++){
      if(list[i].cols.some(c=>shopMatch(c, rec))) return i;
    }
    for(let i = 0; i < list.length; i++){
      const nums = callDays[i][rec.date];
      if(nums && (rec.phone_keys || []).some(k=>nums.has(k))) return i;
    }
    return -1;
  };
}

function shopShares(crm, ring, ym, mode){
  const list = SHOPS.filter(s=>!s.rest);
  const turn = {};
  (crm.deal_categories||[]).forEach(c=>{ turn[c.deal_id] = c.turnover || 0; });
  const owner = shopOwnerFn(crm, ring);

  const out = list.map(s=>({name: s.name, key: s.key, n: 0, v: 0}));
  const rest = {name: 'Інше', key: '__rest', n: 0, v: 0};
  const src = mode === 'leads' ? (crm.leads || []) : (crm.deals || []);
  src.forEach(rec=>{
    if(!(rec.date || '').startsWith(ym)) return;
    const i = owner(rec);
    const bucket = i < 0 ? rest : out[i];
    bucket.n += 1;
    bucket.v += mode === 'leads' ? 1 : (turn[rec.id] || 0);
  });
  if(rest.n) out.push(rest);
  return out;
}

// Чи належить лід цій вкладці. Три шляхи: мітка сайту, власне джерело
// магазину (чат-бот, «Заявка симплер») або дзвінок із його сайту — у
// Bitrix не записано, з якого сайту дзвінок, тому телефон ліда звіряємо
// з дзвінками Ringostat за той самий день. Хеші, не номери.
function leadFilter(shop, crm, ring){
  if(shop.rest) return () => true;
  const callDays = {};
  const callCols = shop.cols.filter(c=>c.callSite);
  if(callCols.length){
    (ring && ring.calls || []).forEach(c=>{
      if(!c.date || !c.caller_key) return;
      if(!callCols.some(col=>col.callSite === c.site)) return;
      (callDays[c.date] = callDays[c.date] || new Set()).add(c.caller_key);
    });
  }
  return l => {
    if(shop.cols.some(c=>shopMatch(c, l))) return true;
    const nums = callDays[l.date];
    return !!nums && (l.phone_keys || []).some(k=>nums.has(k));
  };
}

function shopLeadRows(shop, crm, ring){
  const mine = leadFilter(shop, crm, ring);
  const rows = {};
  (crm.leads||[]).forEach(l=>{
    if(!l.date || !mine(l)) return;
    let key = REST_KEY;
    for(const c of LEAD_COLS){ if(shopMatch(c, l)){ key = c.k; break; } }
    const r = rows[l.date] = rows[l.date] || {};
    const cur = r[key] = r[key] || [0, 0, 0];   // лідів, якісних, дублів
    cur[0] += 1;
    if(l.status_id === LEAD_GOOD) cur[1] += 1;
    if(l.status_id === LEAD_DUP)  cur[2] += 1;
  });
  return rows;
}

// Причини, з яких ліди бракують. Найцінніше в усій таблиці лідів:
// «немає в наявності» — це втрати на залишках, «нет связи» — на обробці,
// і лікуються вони різним.
function leadReasons(shop, crm, ring, ym){
  const mine = leadFilter(shop, crm, ring);
  const out = {};
  (crm.leads||[]).forEach(l=>{
    if(!(l.date || '').startsWith(ym) || !mine(l)) return;
    const name = (l.reject_reason_name || '').trim();
    if(!name) return;
    out[name] = (out[name] || 0) + 1;
  });
  return out;
}

// Той самий набір угод, але розкладений по товарних категоріях.
//
// Чому підсумок рахується не як сума колонок: угода з матрацом і диваном
// потрапляє в обидві, тож кількості по колонках додаються з накладкою.
// А от «Разом» — це ті самі угоди й ті самі гроші, що й у розрізі каналів:
// turnover = сума категорій плюс аксесуари, копійка в копійку.
function shopCatRows(crm, members, turn){
  const rows = {};
  (crm.deal_categories||[]).forEach(c=>{
    const mine = members[c.date];
    if(!mine || !mine.has(String(c.deal_id))) return;
    const r = rows[c.date] = rows[c.date] || {};
    CATS.forEach(cat=>{
      const v = cat.own ? (c[cat.k] || 0) : ((c.categories || {})[cat.k] || 0);
      if(!v) return;
      const cur = r[cat.k] = r[cat.k] || [0, 0];
      cur[0] += 1; cur[1] += v;
    });
  });
  // Разом за день — усі угоди вкладки, а не лише ті, що мають категорії.
  Object.keys(members).forEach(date=>{
    let n = 0, sum = 0;
    members[date].forEach(id=>{ n += 1; sum += turn[id] || 0; });
    (rows[date] = rows[date] || {})['__all'] = [n, sum];
  });
  return rows;
}

// Місяці, за які взагалі є угоди, від найновішого. Більше за півроку не
// показуємо: збір тягне обмежене вікно, старіші місяці все одно порожні.
function shopMonthList(crm){
  const ms = new Set((crm.deals||[]).map(d=>(d.date||'').slice(0,7)).filter(Boolean));
  return [...ms].sort().reverse().slice(0, 6);
}

// Дні вибраного місяця. Для поточного — тільки до сьогодні: порожні клітинки
// на майбутні числа виглядали б як провал, якого ще не було.
function shopDays(ym){
  const [y, m] = ym.split('-').map(Number);
  const today = new Date();
  const last = (y === today.getFullYear() && m === today.getMonth()+1)
      ? today.getDate() : new Date(y, m, 0).getDate();
  const out = [];
  for(let d=1; d<=last; d++) out.push(`${ym}-${String(d).padStart(2,'0')}`);
  return out;
}

function shopSetMonth(ym){ shopMonth = ym; shopOpen = false; renderShop(); }
function shopToggle(){ shopOpen = !shopOpen; renderShop(); }

function shopSetTab(i){
  shopIdx = i; shopOpen = false;
  try{ localStorage.setItem(SHOP_KEY, SHOPS[i].key); }catch(e){}
  renderShop();
}

// ── Джерела трафіку ──────────────────────────────────────────────────────
// «google» і «google.com» — те саме джерело; без зведення вони стоять
// двома рядками і діляться статистикою навпіл.
const utmNorm = s => (String(s||'').trim().replace(/[._]com$/i, '') || '').trim();

function utmRows(crm, ring, ym, shopKey, mode){
  const turn = {};
  (crm.deal_categories||[]).forEach(c=>{ turn[c.deal_id] = c.turnover || 0; });
  const list = SHOPS.filter(s=>!s.rest);
  const want = list.findIndex(s=>s.key === shopKey);   // -1 = усі магазини
  const owner = want >= 0 ? shopOwnerFn(crm, ring) : null;

  const src = mode === 'leads' ? (crm.leads || []) : (crm.deals || []);
  const by = {};
  src.forEach(rec=>{
    if(!(rec.date || '').startsWith(ym)) return;
    if(owner && owner(rec) !== want) return;
    const s = utmNorm(rec.utm_source) || '(не вказано)';
    const m = utmNorm(rec.utm_medium) || '(не вказано)';
    const k = s + '||' + m;
    if(!by[k]) by[k] = {source:s, medium:m, n:0, v:0};
    by[k].n += 1;
    by[k].v += mode === 'leads' ? 0 : (turn[rec.id] || 0);
  });
  const rows = Object.values(by);
  rows.sort((a,b)=> mode === 'leads' ? (b.n - a.n) : (b.v - a.v || b.n - a.n));
  return rows;
}

function renderUtm(){
  // Блоки є тільки на сторінці за посиланням. На головній елементів немає —
  // тихо виходимо, щоб один і той самий файл працював на обох.
  const dealsEl = document.getElementById('utm-deals');
  const leadsEl = document.getElementById('utm-leads');
  if(!dealsEl || !leadsEl || !CRM_RAW || !shopMonth) return;

  const shop = SHOPS[shopIdx] || SHOPS[0];
  const mName = MONTHS_UA[Number(shopMonth.slice(5,7)) - 1];
  const scope = shop.rest ? 'усі магазини' : shop.name;

  const draw = (el, rows, withSum) => {
    if(!rows.length){
      el.innerHTML = `<tbody><tr><td class="utm-empty">За цей місяць даних немає.</td></tr></tbody>`;
      return;
    }
    const totN = rows.reduce((a,r)=>a+r.n, 0);
    const totV = rows.reduce((a,r)=>a+r.v, 0);
    const head = `<thead><tr><th>Джерело</th><th>Канал</th><th class="num">К-сть</th>` +
                 (withSum ? `<th class="num">Сума</th>` : '') + `</tr></thead>`;
    const body = rows.map(r=>
      `<tr><td>${escapeHtml(r.source)}</td><td class="med">${escapeHtml(r.medium)}</td>` +
      `<td class="num"><b>${fmt(r.n)}</b></td>` +
      (withSum ? `<td class="num">${fmt(r.v)} ₴</td>` : '') + `</tr>`).join('');
    const foot = `<tr class="utm-total"><td>Разом</td><td></td>` +
      `<td class="num"><b>${fmt(totN)}</b></td>` +
      (withSum ? `<td class="num"><b>${fmt(totV)} ₴</b></td>` : '') + `</tr>`;
    el.innerHTML = head + `<tbody>${body}${foot}</tbody>`;
  };

  draw(dealsEl, utmRows(CRM_RAW, RING_RAW, shopMonth, shop.key, 'deals'), true);
  draw(leadsEl, utmRows(CRM_RAW, RING_RAW, shopMonth, shop.key, 'leads'), false);

  const note = `${mName} · ${scope} · джерело і канал із UTM-міток`;
  document.getElementById('utm-deals-note').textContent =
    note + ' · сума за товарними рядками, без доставки й накладеного платежу';
  document.getElementById('utm-leads-note').textContent =
    note + ' · у ліда суми немає, тому тільки кількість';
}

// Якщо категорій немає, усі суми на сторінці стануть нулями. Мовчазний
// нуль — найгірший варіант: його читають як «не продавали». Кажемо прямо.
function shopMoneyWarn(){
  if(!CRM_RAW) return '';
  const hasDeals = (CRM_RAW.deals || []).length > 0;
  const hasCats  = (CRM_RAW.deal_categories || []).length > 0;
  if(hasDeals && !hasCats){
    return '<span class="shop-warn">Суми тимчасово недоступні: останній збір не отримав '
         + 'товарні позиції. Кількість замовлень вірна, оборот показано нулем.</span> ';
  }
  return '';
}

function renderShop(){
  const host = document.getElementById('shop-table');
  const note = document.getElementById('shop-note');
  const more = document.getElementById('shop-more');
  const tabs = document.getElementById('shop-months');
  const stabs = document.getElementById('shop-tabs');
  if(!CRM_RAW){ host.innerHTML=''; note.textContent='Завантаження…'; return; }

  const shop = SHOPS[shopIdx] || SHOPS[0];
  stabs.innerHTML = SHOPS.map((s,i)=>
    `<button type="button" class="${i===shopIdx?'on':''}" data-i="${i}">${s.name}</button>`).join('');

  // ── місяці ──
  const months = shopMonthList(CRM_RAW);
  const now = new Date();
  const cur = `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}`;
  if(!months.includes(cur)) months.unshift(cur);
  if(!shopMonth || !months.includes(shopMonth)) shopMonth = months[0];
  tabs.innerHTML = months.map(ym=>{
    const m = Number(ym.slice(5,7)) - 1;
    return `<button type="button" class="${ym===shopMonth?'on':''}" data-ym="${ym}">${MONTHS_UA[m]}</button>`;
  }).join('');

  const base_ = shopRows(shop, CRM_RAW, RING_RAW);
  const {members, turn, siteKnown, hasCall} = base_;
  const cats = shopView === 'cats';
  const leads = shopView === 'leads';
  const rows = leads ? shopLeadRows(shop, CRM_RAW, RING_RAW)
             : cats  ? shopCatRows(CRM_RAW, members, turn)
             : base_.rows;
  document.getElementById('shop-views').innerHTML =
    [['channels','Канали'], ['cats','Категорії'], ['leads','Ліди']].map(([v,label])=>
      `<button type="button" class="${v===shopView?'on':''}" data-v="${v}">${label}</button>`).join('');
  const days = shopDays(shopMonth);
  const shown = shopOpen ? days : days.slice(-SHOP_COLLAPSED);

  const WIDTH = leads ? 3 : 2;      // ліди несуть ще й лічильник дублів
  const zero = () => new Array(WIDTH).fill(0);
  const add = (a, b) => a.map((v, i) => v + (b[i] || 0));
  const get = (d, k) => (rows[d] && rows[d][k]) || zero();
  const monthSum = k => days.reduce((a, d) => add(a, get(d, k)), zero());

  // «Інше» показуємо тільки коли в цьому місяці туди щось справді впало.
  let cols;
  if(leads){
    cols = LEAD_COLS.filter(c=>monthSum(c.k)[0]);
    if(monthSum(REST_KEY)[0]) cols.push({k: REST_KEY, label: 'Інше'});
  }else if(cats){
    cols = CATS.filter(c=>monthSum(c.k)[0]);
  }else{
    cols = shop.cols.slice();
    if(shop.rest && monthSum(REST_KEY)[0]) cols.push({k: REST_KEY, label: 'Інше'});
  }

  const cell = v => !v[0] ? '<span class="zero">—</span>'
    : leads ? `<span class="n">${fmt(v[0])}</span><span class="s">${fmt(v[1])} якісн.</span>`
            : `<span class="n">${fmt(v[0])}</span><span class="s">${fmt(v[1])} ₴</span>`;
  // У розрізі категорій підсумок беремо готовий: складати колонки не можна,
  // бо одна угода може лежати в кількох.
  const rowSum = cats
    ? (d => get(d, '__all'))
    : (d => cols.reduce((a, c) => add(a, get(d, c.k)), zero()));

  const body = shown.map(d=>{
    const [y,m,dd] = d.split('-');
    return `<tr><td>${dd}.${m}</td>`
         + cols.map(c=>`<td>${cell(get(d, c.k))}</td>`).join('')
         + `<td>${cell(rowSum(d))}</td></tr>`;
  }).join('');

  // Підсумок завжди за весь місяць, навіть коли видно п'ять днів: інакше
  // згорнутий вигляд показував би суму, яку ні з чим не звірити.
  const tots = cols.map(c=>monthSum(c.k));
  const all = cats ? monthSum('__all') : tots.reduce(add, zero());
  // Конверсія каналу окремим рядком під підсумком. У клітинку її не
  // запхати: там уже два числа, а третє перетворило б таблицю на кашу.
  // Знаменник — ліди без дублікатів: той самий клієнт, що звернувся двічі,
  // не робить канал гіршим.
  const convCell = v => {
    const base2 = v[0] - (v[2] || 0);
    if(!base2) return '<span class="zero">—</span>';
    const pc = Math.round(v[1] / base2 * 100);
    const cls = pc >= 50 ? ' class="g"' : pc < 25 ? ' class="w"' : '';
    return `<span${cls}>${pc}%</span>`;
  };
  const convRow = !leads ? '' :
    `<tr class="conv"><td>Конверсія в якісні</td>`
    + tots.map(v=>`<td>${convCell(v)}</td>`).join('')
    + `<td>${convCell(all)}</td></tr>`;

  const mName = MONTHS_UA[Number(shopMonth.slice(5,7))-1];
  host.innerHTML = `<thead><tr><th>День</th>`
    + cols.map(c=>`<th>${c.label}</th>`).join('')
    + `<th>Разом</th></tr></thead><tbody>${body}`
    + `<tr class="sum"><td>Разом за ${mName}</td>`
    + tots.map(v=>`<td>${cell(v)}</td>`).join('')
    + `<td>${cell(all)}</td></tr>${convRow}</tbody>`;

  // Кнопка потрібна лише тоді, коли є що ховати.
  const mGen = MONTHS_UA_GEN[Number(shopMonth.slice(5,7))-1];
  more.style.display = days.length > SHOP_COLLAPSED ? '' : 'none';
  more.textContent = shopOpen
    ? `− Згорнути до ${SHOP_COLLAPSED} днів`
    : `+ Усі дні ${mGen} (${days.length})`;

  // Вік обох джерел прямо в шапці: таблиця зводить угоди з дзвінками, і
  // «свіжа CRM плюс учорашні дзвінки» дає цифри, яким не можна вірити, —
  // але на вигляд вони такі самі, як правильні.
  const ageEl = document.getElementById('shop-age');
  const part = (label, stamp, need) => {
    if(!need) return '';
    // ageMin віддає Infinity, коли мітки немає або вона нечитана.
    const m = ageMin(stamp);
    if(!isFinite(m)) return `${label} <b class="old">невідомо</b>`;
    const cls = m > 45 ? ' class="old"' : '';
    return `${label} <b${cls}>${fmtAge(m)}</b>`;
  };
  ageEl.innerHTML = [part('CRM', CRM_RAW.generated_at, true),
                     part('дзвінки', RING_RAW && RING_RAW.generated_at, hasCall)]
                    .filter(Boolean).join(' · ');

  // 1 день / 2 дні / 5 днів — інакше в підписах стоїть «31 днів».
  const plural = (n, one, few, many) => {
    const a = Math.abs(Math.round(n)) % 100, b = a % 10;
    if(a > 10 && a < 20) return many;
    if(b > 1 && b < 5) return few;
    return b === 1 ? one : many;
  };

  // ── середнє й прогноз ──
  // Рахуємо по ЗАКРИТИХ днях: сьогоднішній день ще набирається, і якщо
  // поділити на нього о десятій ранку, середнє піде вниз, а прогноз — за
  // ним. Для минулих місяців закриті всі дні.
  const todayIso = kpiIso(new Date());
  const statDays = days.filter(d => d < todayIso);
  const inMonth = new Date(Number(shopMonth.slice(0,4)), Number(shopMonth.slice(5,7)), 0).getDate();
  const sumOver = list => list.reduce((a,d)=>{
    const v = rowSum(d); return [a[0]+v[0], a[1]+v[1]];
  }, [0,0]);
  const st = sumOver(statDays);
  const statsEl = document.getElementById('shop-stats');
  const reasonsEl = document.getElementById('shop-reasons');
  reasonsEl.innerHTML = '';

  // ── частки магазинів ──
  const sharesEl = document.getElementById('shop-shares');
  const shares = shopShares(CRM_RAW, RING_RAW, shopMonth, leads ? 'leads' : 'money');
  const shTotal = shares.reduce((a,s)=>a+s.v, 0);
  if(shTotal > 0){
    const pcOf = s => s.v / shTotal * 100;
    sharesEl.innerHTML =
      `<h3>Частка магазинів ${leads ? 'у лідах' : 'у обороті'} · ` +
      `${MONTHS_UA[Number(shopMonth.slice(5,7))-1]}</h3>` +
      `<div class="track">` + shares.map((s,i)=>
        `<i style="width:${pcOf(s).toFixed(2)}%;background:${SHARE_COLORS[i % SHARE_COLORS.length]}"></i>`
      ).join('') + `</div><div class="legend">` +
      shares.map((s,i)=>{
        const cur = s.key === shop.key ? ' cur' : '';
        const val = leads ? `${fmt(s.v)} ${plural(s.v,'лід','ліди','лідів')}`
                          : `${fmt(s.v)} ₴`;
        const sub = leads ? '' : ` · ${fmt(s.n)} ${plural(s.n,'угода','угоди','угод')}`;
        return `<div class="it${cur}">` +
          `<span class="dot" style="background:${SHARE_COLORS[i % SHARE_COLORS.length]}"></span>` +
          `<span class="nm">${escapeHtml(s.name)}</span>` +
          `<span class="pc">${pcOf(s).toFixed(pcOf(s) < 10 ? 1 : 0)}%</span>` +
          `<span class="sm">${val}${sub}</span></div>`;
      }).join('') + `</div>`;
  }else{
    sharesEl.innerHTML = '';
  }
  if(leads){
    // Для лідів гроші ні до чого: у Bitrix сума ліда нульова. Рахуємо
    // кількість і якість — і конверсію без дублікатів у знаменнику.
    const n = all[0], good = all[1], dup = all[2];
    const base2 = n - dup;
    const tile = (label, value, sub) =>
      `<div class="st"><div class="l">${label}</div>` +
      `<div class="v">${value}</div><div class="s">${sub}</div></div>`;
    statsEl.innerHTML =
      tile('Лідів за місяць', fmt(n), `${fmt1(n / Math.max(1, statDays.length || days.length))} на день`) +
      tile('Якісних', fmt(good), `${fmt(n - good - dup)} некваліфікованих`) +
      tile('Конверсія', base2 ? Math.round(good / base2 * 100) + '%' : '—', 'без дублікатів') +
      tile('Дублікати', fmt(dup), n ? Math.round(dup / n * 100) + '% усіх лідів' : '—');

    // Причини браку: цього місяця проти попереднього.
    const prevYm = (() => {
      const [y, m] = shopMonth.split('-').map(Number);
      const d0 = new Date(y, m - 2, 1);
      return `${d0.getFullYear()}-${String(d0.getMonth()+1).padStart(2,'0')}`;
    })();
    const cur = leadReasons(shop, CRM_RAW, RING_RAW, shopMonth);
    const prev = leadReasons(shop, CRM_RAW, RING_RAW, prevYm);
    const list = Object.keys(cur).sort((a,b)=>cur[b]-cur[a]).slice(0, 8);
    if(list.length){
      const total = Object.values(cur).reduce((a,b)=>a+b, 0);
      const top = cur[list[0]] || 1;
      reasonsEl.innerHTML = `<h3>Чому бракують ліди · ${MONTHS_UA[Number(shopMonth.slice(5,7))-1]}</h3><ul>` +
        list.map(name=>{
          const was = prev[name] || 0, now = cur[name];
          const diff = was ? Math.round((now - was) / was * 100) : null;
          const cls = diff === null ? 'flat' : diff > 10 ? 'up' : diff < -10 ? 'down' : 'flat';
          const sign = diff === null ? 'новe' : (diff > 0 ? '+' : '') + diff + '%';
          return `<li><span>${escapeHtml(name)}</span>` +
                 `<span class="n">${fmt(now)}</span>` +
                 `<span class="d ${cls}" title="проти ${fmt(was)} минулого місяця">${sign}</span>` +
                 `<span class="bar"><i style="width:${Math.round(now/top*100)}%"></i></span></li>`;
        }).join('') +
        `</ul><div class="s" style="font-size:11px;color:var(--ink-dim);margin-top:10px">` +
        `${fmt(total)} бракованих лідів · відсоток — зміна проти попереднього місяця</div>`;
    }
  }else if(!statDays.length){
    statsEl.innerHTML = '<div class="st"><div class="l">Середні</div>' +
      '<div class="s">місяць тільки почався — рахувати нема по чому</div></div>';
  }else{
    const n = statDays.length;
    const perDay   = st[1] / n;
    const check    = st[0] ? st[1] / st[0] : 0;
    const ordDay   = st[0] / n;
    const done     = n >= inMonth;            // місяць закритий — це вже факт
    const forecast = done ? all[1] : perDay * inMonth;
    const fcOrders = done ? all[0] : ordDay * inMonth;
    const tile = (label, value, sub, cls) =>
      `<div class="st${cls||''}"><div class="l">${label}</div>` +
      `<div class="v">${value}</div><div class="s">${sub}</div></div>`;
    statsEl.innerHTML =
      tile('Середній добовий оборот', fmt(perDay) + ' ₴',
           `за ${n} ${plural(n,'день','дні','днів')}` + (done ? '' : ', без сьогодні')) +
      tile('Середній чек', fmt(check) + ' ₴',
           `${fmt(st[0])} ${plural(st[0],'замовлення','замовлення','замовлень')}`) +
      tile('Замовлень на день', fmt1(ordDay),
           `${inMonth} ${plural(inMonth,'день','дні','днів')} у місяці`) +
      tile(done ? 'Оборот за місяць' : 'Прогноз на місяць', fmt(forecast) + ' ₴',
           done ? 'факт'
                : `очікуємо ${fmt(fcOrders)} ${plural(fcOrders,'замовлення','замовлення','замовлень')}`,
           done ? '' : ' fc');
  }

  let base = 'Сума — товарні рядки, без накладеного платежу й доставки';
  if(leads) base = 'У клітинці — скільки лідів і скільки з них визнали якісними';
  else if(cats) base += ' · угода з матрацом і диваном рахується в обох колонках';
  // У вкладці лідів сум немає, тому попередження про них ні до чого.
  const warn = leads ? '' : shopMoneyWarn();
  if(leads || cats || !hasCall) note.innerHTML = warn + base;
  else if(!RING_RAW)        note.innerHTML = warn + base + ' · дзвінки завантажуються…';
  else if(RING_RAW.failed)  note.innerHTML = warn + base +
      ' · <span style="color:var(--warning)">дзвінки не завантажились</span>';
  else if(!siteKnown)       note.innerHTML = warn + base +
      ' · <span style="color:var(--warning)">Ringostat не віддав сайт дзвінка — колонка «Дзвінки» поки порожня</span>';
  else                      note.innerHTML = warn + base;
}

// Блоки за джерелом трафіку живуть тим самим місяцем і магазином, що й
// таблиця вище, тому перемальовуються разом із нею.
const _renderShopBase = renderShop;
renderShop = function(){ _renderShopBase.apply(this, arguments); try{ renderUtm(); }catch(e){} };

document.getElementById('shop-months').addEventListener('click', e=>{
  const b = e.target.closest('button[data-ym]');
  if(b) shopSetMonth(b.dataset.ym);
});
document.getElementById('shop-tabs').addEventListener('click', e=>{
  const b = e.target.closest('button[data-i]');
  if(b) shopSetTab(Number(b.dataset.i));
});
document.getElementById('shop-views').addEventListener('click', e=>{
  const b = e.target.closest('button[data-v]');
  if(!b || b.dataset.v === shopView) return;
  shopView = b.dataset.v;
  try{ localStorage.setItem(VIEW_KEY, shopView); }catch(e){}
  renderShop();
});
try{
  const saved = localStorage.getItem(SHOP_KEY);
  const i = SHOPS.findIndex(s=>s.key === saved);
  if(i >= 0) shopIdx = i;
  if(localStorage.getItem(VIEW_KEY) === 'cats') shopView = 'cats';
}catch(e){}
