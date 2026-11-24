// Історія по місяцях — з архіву bitrix_history.json.
//
// Навіщо окремий файл. Живе вікно в Бітріксі — 90 днів, і таблиця
// «Замовлення по днях» глибше не бачить. Архів бачить усю історію порталу,
// але в ньому зовсім інший набір полів: день × менеджер, і тільки угоди,
// суми, ліди та брак. Ні каналу, ні сайту, ні категорії, ні товарних рядків
// там немає — тому «та сама таблиця, але за три роки» неможлива в принципі,
// і будувати треба окремий, чесніший блок.
//
// Сума тут ВАЛОВА (OPPORTUNITY), а в таблиці магазинів — за товарними
// рядками, без доставки й накладеного платежу. Це різні числа, і вони не
// зійдуться. Підписуємо прямо, щоб ніхто не шукав неіснуючу розбіжність.
//
// Файл підключається і на головній, і на сторінці CRM. Якщо контейнера на
// сторінці немає — скрипт мовчки нічого не робить.

const HIST_FROM = '2025-01';   // 2023-11 — міграційний дамп, 2024 не потрібен

(function () {
  const host = document.getElementById('hist-months');
  if (!host) return;

  const style = document.createElement('style');
  style.textContent = `
    .hm-wrap{overflow-x:auto}
    table.hm{width:100%;border-collapse:collapse;font-size:13px}
    table.hm th{font-size:10.5px;text-transform:uppercase;letter-spacing:.7px;
      font-weight:600;color:var(--ink-dim);text-align:right;padding:0 10px 10px;white-space:nowrap}
    table.hm th:first-child{text-align:left}
    table.hm td{padding:9px 10px;border-top:1px solid var(--border);text-align:right;white-space:nowrap}
    table.hm td:first-child{text-align:left;color:var(--ink)}
    table.hm tr:hover td{background:rgba(255,255,255,.02)}
    .hm-bar{position:relative}
    .hm-bar i{position:absolute;left:10px;right:10px;bottom:3px;height:2px;
      background:rgba(255,255,255,.07);border-radius:2px;display:block}
    .hm-bar i b{position:absolute;left:0;top:0;bottom:0;border-radius:2px;
      background:linear-gradient(90deg,var(--accent),var(--accent-2));display:block}
    .hm-up{color:var(--good,#27E37A)} .hm-down{color:var(--bad,#FF5E6C)}
    .hm-part{color:var(--warning,#FFC84D);font-size:11px;font-weight:600}
    .hm-year td{border-top:1px solid var(--border-strong);font-weight:700;
      background:rgba(255,255,255,.025)}
    .hm-note{font-size:11.5px;color:var(--ink-dim);margin-top:10px;line-height:1.5}
  `;
  document.head.appendChild(style);

  const nf = n => new Intl.NumberFormat('uk-UA').format(Math.round(n || 0));
  const MON = ['січень','лютий','березень','квітень','травень','червень',
               'липень','серпень','вересень','жовтень','листопад','грудень'];

  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  function daysInMonth(ym) {
    const y = +ym.slice(0, 4), m = +ym.slice(5, 7);
    return new Date(y, m, 0).getDate();
  }

  // Скільки днів цього місяця взагалі могли бути зібрані (для поточного —
  // до вчора). Порівнюємо з тим, скільки днів реально є в архіві: так
  // видно обрізані місяці, а не просто «мало продали».
  function expectedDays(ym) {
    const now = new Date();
    const cur = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
    if (ym > cur) return 0;
    if (ym === cur) return Math.max(0, now.getDate() - 1);
    return daysInMonth(ym);
  }

  function render(hist) {
    const done = new Set(hist.months_done || []);
    const by = {};
    (hist.rows || []).forEach(r => {
      const ym = String(r.date || '').slice(0, 7);
      if (!ym || ym < HIST_FROM) return;
      const m = by[ym] || (by[ym] = {deals: 0, sum: 0, leads: 0, lost: 0, days: new Set()});
      m.deals += r.deals || 0;
      m.sum += r.sum || 0;
      m.leads += r.leads || 0;
      m.lost += r.leads_lost || 0;
      m.days.add(r.date);
    });

    const months = Object.keys(by).sort();
    if (!months.length) {
      host.innerHTML = '<div class="hm-note">Архів порожній або ще не зібраний.</div>';
      return;
    }
    const maxSum = Math.max(1, ...months.map(m => by[m].sum));

    const rows = [];
    let curYear = null, acc = null;
    const yearRow = (y, a) => `<tr class="hm-year"><td>${y} — разом</td>
      <td>${nf(a.deals)}</td><td>${nf(a.sum)} ₴</td>
      <td>${nf(a.sum / Math.max(1, a.deals))} ₴</td>
      <td>${nf(a.leads)}</td><td>${nf(a.lost)}</td>
      <td>${a.leads ? (a.deals / a.leads * 100).toFixed(1) : '—'}%</td><td></td></tr>`;

    months.forEach(ym => {
      const y = ym.slice(0, 4);
      if (curYear !== null && y !== curYear) { rows.push(yearRow(curYear, acc)); acc = null; }
      if (!acc) { curYear = y; acc = {deals: 0, sum: 0, leads: 0, lost: 0}; }
      const v = by[ym];
      acc.deals += v.deals; acc.sum += v.sum; acc.leads += v.leads; acc.lost += v.lost;

      const prev = by[`${+y - 1}-${ym.slice(5, 7)}`];
      let yoy = '<span style="color:var(--ink-dim)">—</span>';
      if (prev && prev.sum > 0) {
        const d = (v.sum / prev.sum - 1) * 100;
        yoy = `<span class="${d >= 0 ? 'hm-up' : 'hm-down'}">${d >= 0 ? '+' : ''}${d.toFixed(0)}%</span>`;
      }

      // Місяць неповний, якщо днів у архіві менше, ніж могло бути. Саме так
      // видно липень–серпень 2026, де між архівом і живим вікном діра.
      const exp = expectedDays(ym);
      const have = v.days.size;
      const partial = exp > 0 && have < exp - 1;
      const mark = partial
        ? ` <span class="hm-part" title="В архіві лише ${have} днів із ${exp}">неповний</span>`
        : (!done.has(ym) ? ' <span class="hm-part" title="Місяць ще не закрито">триває</span>' : '');

      rows.push(`<tr>
        <td>${MON[+ym.slice(5, 7) - 1]} ${y}${mark}</td>
        <td>${nf(v.deals)}</td>
        <td class="hm-bar">${nf(v.sum)} ₴<i><b style="width:${(v.sum / maxSum * 100).toFixed(1)}%"></b></i></td>
        <td>${nf(v.sum / Math.max(1, v.deals))} ₴</td>
        <td>${nf(v.leads)}</td>
        <td>${nf(v.lost)}</td>
        <td>${v.leads ? (v.deals / v.leads * 100).toFixed(1) : '—'}%</td>
        <td>${yoy}</td>
      </tr>`);
    });
    if (acc) rows.push(yearRow(curYear, acc));

    host.innerHTML = `<div class="hm-wrap"><table class="hm">
      <thead><tr><th>Місяць</th><th>Угоди</th><th>Оборот</th><th>Чек</th>
      <th>Ліди</th><th>Брак</th><th>Конверсія</th><th>Оборот до торік</th></tr></thead>
      <tbody>${rows.join('')}</tbody></table></div>
      <div class="hm-note">Сума — <b>валова</b> (поле «Сума» угоди), а не за товарними
      рядками: архів зберігає тільки її, тому з таблицею «Замовлення по днях» ці
      цифри не зійдуться, і це нормально. Розрізу по каналах, сайтах і категоріях
      в архіві немає. Дані з ${esc(hist.from || '—')} по ${esc(hist.to || '—')},
      зібрано ${esc(hist.generated_at || '—')}.</div>`;
  }

  fetch('bitrix_history.json?_=' + Date.now())
    .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
    .then(render)
    .catch(e => {
      host.innerHTML = `<div class="hm-note">Архів історії не завантажився
        (${esc(e.message)}). Він збирається окремим воркфлоу
        «Bitrix History Archive» і лежить у файлі bitrix_history.json.</div>`;
    });
})();
