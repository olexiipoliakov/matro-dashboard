/* camp-filter.js — фільтр таблиці кампаній: усі / активні / вимкнені.
 *
 * У кабінеті половина кампаній зупинена, і в таблиці вони перемішані з
 * робочими: щоб побачити, що крутиться зараз, доводиться вишукувати зелені
 * крапки очима. Ця кнопка просто ховає зайві рядки.
 *
 * Чому фільтр, а не сортування. Відсортувати за статусом мало: вимкнені
 * однаково лишаються в таблиці й займають екран. Потрібно їх прибрати.
 *
 * Чому окремий файл: сторінка index.html і так велика, а це самостійна
 * дрібниця. Підключати ПІСЛЯ основного скрипта сторінки — файл спирається
 * на її глобальні CAMPS і renderTable().
 */
(function () {
  'use strict';

  var MODE_KEY = 'camp-filter-mode';
  var mode = 'all';                       // all | active | paused
  try { mode = localStorage.getItem(MODE_KEY) || 'all'; } catch (e) {}

  var CSS = ''
    + '.cf-bar{display:flex;align-items:center;gap:8px;margin:0 0 14px;flex-wrap:wrap}'
    + '.cf-bar .cf-label{font-size:11px;text-transform:uppercase;letter-spacing:.9px;'
    + 'color:var(--ink-dim);font-weight:600;margin-right:4px}'
    + '.cf-btn{font-family:inherit;font-size:12.5px;font-weight:600;color:var(--ink-soft);'
    + 'background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:9px;'
    + 'padding:7px 13px;cursor:pointer;transition:all 160ms ease;display:inline-flex;'
    + 'align-items:center;gap:7px}'
    + '.cf-btn:hover{color:var(--ink);border-color:var(--border-strong)}'
    + '.cf-btn.on{color:var(--ink);background:rgba(255,140,66,.14);border-color:rgba(255,140,66,.4)}'
    + '.cf-btn .cf-n{font-variant-numeric:tabular-nums;color:var(--ink-dim)}'
    + '.cf-btn.on .cf-n{color:var(--accent)}'
    + '.cf-dot{width:7px;height:7px;border-radius:50%;display:inline-block}'
    + '.cf-dot.a{background:var(--success)}.cf-dot.p{background:var(--warning)}';

  // Статус кампанії: ACTIVE — працює, решта (PAUSED, ARCHIVED…) — ні.
  function isActive(status) {
    return !status || String(status).toUpperCase() === 'ACTIVE';
  }

  function statusMap() {
    var m = {};
    if (typeof CAMPS !== 'undefined' && CAMPS) {
      CAMPS.forEach(function (c) { m[c.campaign] = c.status; });
    }
    return m;
  }

  function counts() {
    var a = 0, p = 0;
    if (typeof CAMPS !== 'undefined' && CAMPS) {
      CAMPS.forEach(function (c) { isActive(c.status) ? a++ : p++; });
    }
    return {all: a + p, active: a, paused: p};
  }

  function keep(status) {
    if (mode === 'active') return isActive(status);
    if (mode === 'paused') return !isActive(status);
    return true;
  }

  function apply() {
    var map = statusMap();

    // ── рядки таблиці ──
    var rows = document.querySelectorAll('#camp-body tr[data-camp]');
    var shown = [];
    Array.prototype.forEach.call(rows, function (tr) {
      var name = tr.dataset.camp;
      var ok = keep(map[name]);
      tr.style.display = ok ? '' : 'none';
      if (ok) shown.push(name);
    });

    // ── картки на телефоні (у них немає data-camp, шукаємо за назвою) ──
    Array.prototype.forEach.call(document.querySelectorAll('#camp-cards .mob-card'), function (card) {
      var el = card.querySelector('.mob-card-name');
      var name = el ? el.textContent.trim() : '';
      card.style.display = keep(map[name]) ? '' : 'none';
    });

    // ── підсумковий рядок ──
    // Якщо частину кампаній сховали, старе «ИТОГО» показувало б суму по
    // всіх — тобто більше, ніж видно в таблиці. Перераховуємо по тому,
    // що лишилось на екрані.
    var tot = document.querySelector('#camp-body tr.tot-row');
    if (tot && typeof CAMPS !== 'undefined' && CAMPS) {
      if (mode === 'all') {
        tot.style.display = '';
      } else {
        var sel = CAMPS.filter(function (c) { return shown.indexOf(c.campaign) >= 0; });
        var spend = sel.reduce(function (a, c) { return a + c.spend; }, 0);
        var leads = sel.reduce(function (a, c) { return a + c.leads; }, 0);
        var all = CAMPS.reduce(function (a, c) { return a + c.spend; }, 0);
        var f = (typeof fmt === 'function') ? fmt : function (v) { return Math.round(v); };
        var cells = tot.cells;
        if (cells.length >= 7) {
          cells[0].textContent = 'ИТОГО (' + (mode === 'active' ? 'активные' : 'выключенные') + ')';
          cells[3].textContent = f(spend);
          cells[4].textContent = f(leads);
          cells[5].textContent = leads ? f(Math.round(spend / leads)) + ' грн' : '—';
          cells[6].textContent = all ? (spend / all * 100).toFixed(0) + '%' : '0%';
        }
        tot.style.display = '';
      }
    }

    // ── лічильники на кнопках ──
    var c = counts();
    ['all', 'active', 'paused'].forEach(function (k) {
      var btn = document.querySelector('.cf-btn[data-mode="' + k + '"]');
      if (!btn) return;
      btn.classList.toggle('on', k === mode);
      var n = btn.querySelector('.cf-n');
      if (n) n.textContent = c[k];
    });
  }

  function setMode(m) {
    mode = m;
    try { localStorage.setItem(MODE_KEY, m); } catch (e) {}
    // Перемальовуємо таблицю з нуля: інакше після показу «усіх» у
    // підсумковому рядку лишились би числа попереднього фільтра.
    if (typeof window.renderTable === 'function' && typeof sKey !== 'undefined') {
      window.renderTable(sKey, sAsc);
    } else {
      apply();
    }
  }

  function init() {
    var table = document.getElementById('camp-table');
    if (!table || document.querySelector('.cf-bar')) return;

    var style = document.createElement('style');
    style.textContent = CSS;
    document.head.appendChild(style);

    var bar = document.createElement('div');
    bar.className = 'cf-bar';
    bar.innerHTML =
      '<span class="cf-label">Показывать</span>'
      + '<button type="button" class="cf-btn" data-mode="all">Все <span class="cf-n">0</span></button>'
      + '<button type="button" class="cf-btn" data-mode="active">'
      + '<span class="cf-dot a"></span>Активные <span class="cf-n">0</span></button>'
      + '<button type="button" class="cf-btn" data-mode="paused">'
      + '<span class="cf-dot p"></span>Выключенные <span class="cf-n">0</span></button>';
    // Над панеллю з таблицею, під заголовком розділу.
    var panel = table.closest('.panel') || table.parentElement;
    panel.parentNode.insertBefore(bar, panel);

    bar.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('.cf-btn') : null;
      if (btn) setMode(btn.dataset.mode);
    });

    // renderTable() сторінка кличе при сортуванні й при зміні періоду —
    // після кожного такого перемальовування фільтр треба накласти знову.
    if (typeof window.renderTable === 'function') {
      var orig = window.renderTable;
      window.renderTable = function () {
        var out = orig.apply(this, arguments);
        try { apply(); } catch (err) { console.warn('[camp-filter]', err); }
        return out;
      };
    }
    apply();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
