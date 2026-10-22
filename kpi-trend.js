/* kpi-trend.js — динаміка показника під картками KPI.
 *
 * Клік по картці розкриває лінійний графік саме цього числа по днях
 * ОБРАНОГО періоду, з підказкою «дата + значення» при наведенні.
 *
 * Один файл на дві сторінки. Малювання графіка, підказка й поведінка
 * карток тут спільні, а різниця між сторінками зведена до адаптера:
 * які картки є на сторінці, звідки брати рядки даних і як рахувати
 * показник за день. Інакше довелось би тримати дві копії того самого
 * коду й правити кожну зміну двічі.
 *
 *   index.html (SMM)  — картки Meta Ads, дані з FD
 *   home.html (головна) — картки CRM, дані з CRM_RAW
 *
 * Підключати ПІСЛЯ основного скрипта сторінки: файл спирається на її
 * глобальні змінні й підміняє її ж функцію перемальовування.
 */
(function () {
  'use strict';

  var current = null;   // який показник розкритий
  var drawn = null;     // дані останнього графіка — для перемальовування
  var A = null;         // активний адаптер сторінки

  // ── адаптер 1: сторінка SMM ────────────────────────────────────────────
  // goals — цілі кампаній, які входять у показник. Ціна за листування
  // рахується ТІЛЬКИ по кампаніях на листування: якщо ділити витрати всіх
  // кампаній на результати однієї цілі, ціна завищується в рази.
  var SMM = {
    cards: {
      'k-spend':'spend', 'k-cpl-conv':'conv_cpl', 'k-leads-conv':'conv_count',
      'k-cpl-purch':'purch_cpl', 'k-leads-purch':'purch_count'
    },
    metrics: {
      spend:       {title:'Расход по дням',            unit:'грн', color:'#E06A1F',
                    goals:null, value:function(r){return r.spend;}},
      conv_cpl:    {title:'Цена за переписку по дням', unit:'грн', color:'#4A76D6',
                    goals:['Переписки','Лиды'], ratio:true,
                    value:function(r){return r.leads?r.spend/r.leads:0;}},
      conv_count:  {title:'Переписки по дням',         unit:'',    color:'#1CA478',
                    goals:['Переписки','Лиды'], value:function(r){return r.leads;}},
      purch_cpl:   {title:'Цена за покупку по дням',   unit:'грн', color:'#4A76D6',
                    goals:['Покупки'], ratio:true,
                    value:function(r){return r.leads?r.spend/r.leads:0;}},
      purch_count: {title:'Покупки по дням',           unit:'',    color:'#1CA478',
                    goals:['Покупки'], value:function(r){return r.leads;}}
    },
    hook: 'renderCharts',       // сторінка кличе її після перерахунку даних
    ready: function () { return typeof FD !== 'undefined'; },
    // Повертає {дата: {spend, leads}} за тим самим фільтром, що й картки.
    byDate: function (m) {
      var rows = (typeof FD !== 'undefined' && FD) ? FD : [];
      if (typeof activeCamps !== 'undefined' && activeCamps && activeCamps.size > 0) {
        rows = rows.filter(function (r) { return activeCamps.has(r.campaign); });
      }
      if (m.goals) {
        rows = rows.filter(function (r) { return m.goals.indexOf(r.goal || 'Обращения') >= 0; });
      }
      var out = {};
      rows.forEach(function (r) {
        if (!out[r.date]) out[r.date] = {spend: 0, leads: 0};
        out[r.date].spend += r.spend || 0;
        out[r.date].leads += r.leads || 0;
      });
      return out;
    },
    note: function () {
      return (typeof activeCamps !== 'undefined' && activeCamps && activeCamps.size > 0)
        ? ' · только выбранные кампании' : '';
    }
  };

  // ── адаптер 2: головна (CRM) ───────────────────────────────────────────
  var HOME = {
    cards: {
      'kpi-turnover':'turnover', 'kpi-deals':'deals',
      'kpi-leads':'leads', 'kpi-conv':'conv'
    },
    metrics: {
      turnover: {title:'Оборот по дням',    unit:'₴', color:'#FF8C42',
                 value:function(r){return r.sum;}},
      deals:    {title:'Угоди по дням',     unit:'',  color:'#9B7FE8',
                 value:function(r){return r.deals;}},
      leads:    {title:'Ліди по дням',      unit:'',  color:'#32D8FF',
                 value:function(r){return r.leads;}},
      conv:     {title:'Конверсія по дням', unit:'%', color:'#5EE7B0', ratio:true,
                 value:function(r){return r.leads?r.deals/r.leads*100:0;}}
    },
    hook: 'renderKpi',
    ready: function () { return typeof CRM_RAW !== 'undefined' && CRM_RAW; },
    byDate: function () {
      if (typeof CRM_RAW === 'undefined' || !CRM_RAW) return {};
      var stages = (CRM_RAW.meta && CRM_RAW.meta.stages) || {};
      var dropNames = (typeof KPI_DROP_NAMES !== 'undefined') ? KPI_DROP_NAMES : [];
      var dropped = function (d) {
        return dropNames.indexOf(String((stages[d.stage_id] || {}).name || '')
          .trim().toLowerCase()) >= 0;
      };
      // Оборот рахуємо так само, як картка: сума товарних рядків угоди.
      var turnoverById = {};
      (CRM_RAW.deal_categories || []).forEach(function (c) {
        turnoverById[c.deal_id] = c.turnover || 0;
      });
      var out = {};
      var cell = function (date) {
        if (!out[date]) out[date] = {sum: 0, deals: 0, leads: 0};
        return out[date];
      };
      (CRM_RAW.deals || []).forEach(function (d) {
        if (!d.date || dropped(d)) return;   // відмови й помилкові — повз оборот
        var c = cell(d.date);
        c.deals += 1;
        c.sum += turnoverById[d.id] || 0;
      });
      (CRM_RAW.leads || []).forEach(function (l) {
        if (!l.date) return;
        cell(l.date).leads += 1;
      });
      return out;
    },
    // Період на головній — здебільшого ОДИН день («Сьогодні»), а графік з
    // однієї точки нічого не показує. Тому для одного дня малюємо 30 днів
    // до нього включно й пишемо це в заголовку, щоб не здавалось, ніби
    // графік не слухається вибраної дати.
    window: function (bydate) {
      var from = (typeof KPI_FROM !== 'undefined') ? KPI_FROM : null;
      var to = (typeof KPI_TO !== 'undefined') ? KPI_TO : null;
      var all = Object.keys(bydate).sort();
      if (!all.length) return {dates: [], note: ''};
      var inRange = all.filter(function (d) {
        return (!from || d >= from) && (!to || d <= to);
      });
      if (inRange.length >= 2) return {dates: inRange, note: ''};
      var end = to || from || all[all.length - 1];
      var tail = all.filter(function (d) { return d <= end; }).slice(-30);
      return {dates: tail, note: ' · последние 30 дней'};
    },
    note: function () { return ''; }
  };

  var CSS = ''
    + '.kpi[data-metric],.kpi-card[data-metric]{cursor:pointer;'
    + 'transition:border-color 180ms ease,transform 180ms ease}'
    + '.kpi[data-metric]:hover,.kpi-card[data-metric]:hover{border-color:var(--border-strong);transform:translateY(-2px)}'
    + '.kpi[data-metric]:focus-visible,.kpi-card[data-metric]:focus-visible{outline:2px solid var(--accent);outline-offset:2px}'
    + '.kpi.sel,.kpi-card.sel{border-color:rgba(255,140,66,.55)}'
    + '.kt-hint{position:absolute;right:14px;bottom:12px;font-size:10.5px;color:var(--ink-dim);'
    + 'opacity:0;transition:opacity 180ms ease;pointer-events:none}'
    + '[data-metric]:hover .kt-hint{opacity:.85}.sel .kt-hint{opacity:.95;color:var(--accent)}'
    + '.kpi-trend{display:none;margin:-4px 0 24px}.kpi-trend.open{display:block}'
    + '.kpi-trend .kt-panel{background:rgba(16,21,34,.82);border:1px solid var(--border);'
    + 'border-radius:18px;padding:20px}'
    + '.kpi-trend-head{display:flex;align-items:center;gap:12px;margin-bottom:14px;flex-wrap:wrap}'
    + '.kpi-trend-head h3{font-size:14px;font-weight:600;letter-spacing:-.2px}'
    + '.kpi-trend-head .period{font-size:11.5px;color:var(--ink-dim)}'
    + '.kpi-trend-head .close{margin-left:auto;font-family:inherit;font-size:12px;font-weight:600;'
    + 'color:var(--ink-dim);background:rgba(255,255,255,.04);border:1px solid var(--border);'
    + 'border-radius:9px;padding:6px 12px;cursor:pointer}'
    + '.kpi-trend-head .close:hover{color:var(--ink);border-color:var(--border-strong)}'
    + '.ln-wrap{position:relative;height:230px;user-select:none}'
    + '.ln-wrap svg{display:block;width:100%;height:100%;overflow:visible}'
    + '.ln-xaxis{display:flex;justify-content:space-between;margin-top:8px;font-size:10.5px;color:var(--ink-dim)}'
    + '.ln-foot{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;'
    + 'margin-top:12px;font-size:11.5px;color:var(--ink-dim)}'
    + '.ln-tip{position:absolute;pointer-events:none;opacity:0;transition:opacity 120ms ease;'
    + 'background:#1a2033;border:1px solid var(--border-strong);border-radius:10px;padding:8px 11px;'
    + 'font-size:12px;line-height:1.45;white-space:nowrap;z-index:5;'
    + 'box-shadow:0 10px 24px rgba(0,0,0,.45);transform:translate(-50%,-115%)}'
    + '.ln-tip.on{opacity:1}.ln-tip .d{color:var(--ink-dim);font-size:11px;margin-bottom:2px}'
    + '.ln-tip .v{font-weight:700;font-variant-numeric:tabular-nums}'
    + '.kpi-trend .empty-note{padding:28px 4px;text-align:center;color:var(--ink-dim);font-size:13px}';

  var HTML = ''
    + '<div class="kt-panel">'
    + '  <div class="kpi-trend-head">'
    + '    <h3 id="kt-title">—</h3><span class="period" id="kt-period"></span>'
    + '    <button class="close" type="button" id="kt-close">Закрыть ×</button>'
    + '  </div>'
    + '  <div class="ln-wrap" id="kt-chart"></div>'
    + '  <div class="ln-xaxis" id="kt-xaxis"></div>'
    + '  <div class="ln-foot" id="kt-legend"></div>'
    + '</div>';

  function money(v) {
    return (typeof fmt === 'function') ? fmt(v)
      : new Intl.NumberFormat('uk-UA').format(Math.round(v || 0));
  }

  // Групування точок: понад 31 точку підписи дат зливаються, тому довгі
  // періоди зводимо по тижнях.
  function bucket(dates, bydate, keys) {
    var sum = function (acc, d) {
      keys.forEach(function (k) { acc[k] = (acc[k] || 0) + (bydate[d][k] || 0); });
      return acc;
    };
    if (dates.length <= 31) {
      return {
        labels: dates.map(function (d) { return d.slice(8) + '.' + d.slice(5, 7); }),
        rows: dates.map(function (d) { return bydate[d]; }),
        starts: dates, weekly: false
      };
    }
    var wk = {}, order = [];
    dates.forEach(function (d) {
      var dt = new Date(d + 'T00:00:00');
      var jan = new Date(dt.getFullYear(), 0, 1);
      var wn = Math.ceil((((dt - jan) / 86400000) + jan.getDay() + 1) / 7);
      var key = dt.getFullYear() + '-W' + String(wn).padStart(2, '0');
      if (!wk[key]) { wk[key] = {first: d}; order.push(key); }
      sum(wk[key], d);
    });
    order.sort();
    return {
      labels: order.map(function (k) { var f = wk[k].first; return f.slice(8) + '.' + f.slice(5, 7); }),
      rows: order.map(function (k) { return wk[k]; }),
      starts: order.map(function (k) { return wk[k].first; }),
      weekly: true
    };
  }

  var MONTHS = ['января','февраля','марта','апреля','мая','июня',
                'июля','августа','сентября','октября','ноября','декабря'];
  function humanDate(iso) {
    if (!iso) return '';
    return parseInt(iso.slice(8, 10), 10) + ' ' + (MONTHS[parseInt(iso.slice(5, 7), 10) - 1] || '');
  }

  function toggle(metric) {
    current = (current === metric) ? null : metric;
    render();
    if (current) {
      var box = document.getElementById('kpi-trend');
      if (box) box.scrollIntoView({behavior: 'smooth', block: 'nearest'});
    }
  }

  function render() {
    var box = document.getElementById('kpi-trend');
    if (!box || !A) return;
    Array.prototype.forEach.call(document.querySelectorAll('[data-metric]'), function (el) {
      el.classList.toggle('sel', el.dataset.metric === current);
    });
    if (!current) { box.classList.remove('open'); drawn = null; return; }

    var m = A.metrics[current];
    box.classList.add('open');
    document.getElementById('kt-title').textContent = m.title;

    var bydate = A.byDate(m) || {};
    var win = A.window ? A.window(bydate) : {dates: Object.keys(bydate).sort(), note: ''};
    var dates = win.dates;

    var chart = document.getElementById('kt-chart');
    var legend = document.getElementById('kt-legend');
    if (!dates.length) {
      chart.innerHTML = '<div class="empty-note">За выбранный период по этому показателю данных нет.</div>';
      document.getElementById('kt-period').textContent = '';
      document.getElementById('kt-xaxis').innerHTML = '';
      legend.style.display = 'none';
      drawn = null;
      return;
    }
    legend.style.display = '';

    var keys = Object.keys(bydate[dates[0]]);
    var b = bucket(dates, bydate, keys);
    var values = b.rows.map(m.value);
    var fmtFn = function (v) {
      return money(Math.round(v)) + (m.unit ? (m.unit === '%' ? ' %' : ' ' + m.unit) : '');
    };

    var from = dates[0], to = dates[dates.length - 1];
    document.getElementById('kt-period').textContent =
      from.slice(8) + '.' + from.slice(5, 7) + ' — ' + to.slice(8) + '.' + to.slice(5, 7)
      + (b.weekly ? ' · по неделям' : ' · по дням')
      + (win.note || '') + (A.note ? A.note() : '');

    // Пунктир. Для показників-відношень (ціна за результат, конверсія) це
    // значення за весь період, як у картці, а не середнє денних: дні з
    // великою кількістю результатів важать більше, і графік розходився б
    // із карткою без пояснення.
    var avg;
    if (m.ratio) {
      var tot = {};
      b.rows.forEach(function (r) {
        keys.forEach(function (k) { tot[k] = (tot[k] || 0) + (r[k] || 0); });
      });
      avg = m.value(tot);
    } else {
      avg = values.length ? values.reduce(function (a, v) { return a + v; }, 0) / values.length : 0;
    }
    legend.innerHTML = '<span>Наведите на график, чтобы увидеть дату и значение.</span>'
      + '<span>Пунктир — ' + (m.ratio ? 'значение за весь период' : 'среднее за день')
      + ': ' + fmtFn(avg) + '</span>';

    drawn = {values: values, labels: b.labels, starts: b.starts, weekly: b.weekly,
             color: m.color, fmtFn: fmtFn, avg: avg};
    draw();
  }

  function draw() {
    var box = document.getElementById('kt-chart');
    if (!box || !drawn) return;
    var values = drawn.values, color = drawn.color;
    var W = box.clientWidth || 900, H = box.clientHeight || 230;
    var padL = 10, padR = 10, padT = 20, padB = 12;
    // Нуль завжди на осі: лінія, що починається не з нуля, перебільшує
    // коливання в рази — на дашборді це читається як провал там, де його нема.
    var max = Math.max.apply(null, [1].concat(values));
    var x = function (i) {
      return padL + (values.length > 1 ? i * (W - padL - padR) / (values.length - 1) : (W - padL - padR) / 2);
    };
    var y = function (v) { return padT + (1 - v / max) * (H - padT - padB); };

    var d = '';
    values.forEach(function (v, i) { d += (i ? 'L' : 'M') + x(i).toFixed(1) + ',' + y(v).toFixed(1) + ' '; });
    if (values.length === 1) {
      d = 'M' + (x(0) - 14).toFixed(1) + ',' + y(values[0]).toFixed(1)
        + ' L' + (x(0) + 14).toFixed(1) + ',' + y(values[0]).toFixed(1) + ' ';
    }
    var area = d + 'L' + x(values.length - 1).toFixed(1) + ',' + (H - padB).toFixed(1)
                 + ' L' + x(0).toFixed(1) + ',' + (H - padB).toFixed(1) + ' Z';
    var gid = 'ktg' + Math.random().toString(36).slice(2, 7);
    var avgY = y(drawn.avg).toFixed(1);

    box.innerHTML =
      '<svg>'
      + '<defs><linearGradient id="' + gid + '" x1="0" y1="0" x2="0" y2="1">'
      + '<stop offset="0%" stop-color="' + color + '" stop-opacity="0.30"/>'
      + '<stop offset="100%" stop-color="' + color + '" stop-opacity="0"/>'
      + '</linearGradient></defs>'
      + '<line x1="' + padL + '" x2="' + (W - padR) + '" y1="' + avgY + '" y2="' + avgY + '"'
      + ' stroke="rgba(255,255,255,.22)" stroke-width="1" stroke-dasharray="5 5"/>'
      + '<path d="' + area + '" fill="url(#' + gid + ')"/>'
      + '<path d="' + d + '" fill="none" stroke="' + color + '" stroke-width="2.5"'
      + ' stroke-linecap="round" stroke-linejoin="round"'
      + ' style="filter:drop-shadow(0 0 7px ' + color + '80)"/>'
      + '<line class="kt-cross" x1="0" x2="0" y1="' + padT + '" y2="' + (H - padB) + '"'
      + ' stroke="rgba(255,255,255,.28)" stroke-width="1" style="opacity:0"/>'
      + '<circle class="kt-dot" r="5" fill="' + color + '" stroke="#0B0F1A" stroke-width="2" style="opacity:0"/>'
      + '</svg><div class="ln-tip" id="kt-tip"></div>';

    // Підписи дат: не більше шести, інакше зливаються. Точну дату дає підказка.
    var ax = document.getElementById('kt-xaxis');
    if (ax) {
      var step = Math.max(1, Math.ceil(drawn.labels.length / 6));
      ax.innerHTML = drawn.labels.filter(function (_, i) {
        return i % step === 0 || i === drawn.labels.length - 1;
      }).map(function (l) { return '<span>' + l + '</span>'; }).join('');
    }

    var svg = box.querySelector('svg');
    var cross = box.querySelector('.kt-cross');
    var dot = box.querySelector('.kt-dot');
    var tip = box.querySelector('#kt-tip');

    svg.addEventListener('pointermove', function (e) {
      var r = svg.getBoundingClientRect();
      var rel = (e.clientX - r.left) * W / r.width;
      var step = values.length > 1 ? (W - padL - padR) / (values.length - 1) : 1;
      var i = Math.max(0, Math.min(values.length - 1, Math.round((rel - padL) / step)));
      var px = x(i), py = y(values[i]);
      cross.setAttribute('x1', px); cross.setAttribute('x2', px); cross.style.opacity = '1';
      dot.setAttribute('cx', px); dot.setAttribute('cy', py); dot.style.opacity = '1';
      tip.innerHTML = '<div class="d">' + (drawn.weekly ? 'неделя с ' : '')
                    + (humanDate(drawn.starts[i]) || drawn.labels[i]) + '</div>'
                    + '<div class="v">' + drawn.fmtFn(values[i]) + '</div>';
      tip.style.left = Math.min(Math.max(px, 60), W - 60) + 'px';
      tip.style.top = py + 'px';
      tip.classList.add('on');
    });
    svg.addEventListener('pointerleave', function () {
      cross.style.opacity = '0'; dot.style.opacity = '0'; tip.classList.remove('on');
    });
  }

  function init() {
    if (document.getElementById('kpi-trend')) return;

    // Яка це сторінка — визначаємо по картках, а не по імені файлу:
    // сторінку можуть перейменувати, id показників стабільні.
    var adapters = [SMM, HOME], row = null;
    for (var n = 0; n < adapters.length && !row; n++) {
      var a = adapters[n], found = null;
      Object.keys(a.cards).forEach(function (id) {
        var val = document.getElementById(id);
        var card = val && val.closest ? val.closest('.kpi, .kpi-card') : null;
        if (card && !found) found = card;
      });
      if (found) { A = a; row = found.parentElement; }
    }
    if (!A || !row) return;   // чужа сторінка — тихо нічого не робимо

    var style = document.createElement('style');
    style.textContent = CSS;
    document.head.appendChild(style);

    Object.keys(A.cards).forEach(function (id) {
      var val = document.getElementById(id);
      var card = val && val.closest ? val.closest('.kpi, .kpi-card') : null;
      if (!card) return;
      card.dataset.metric = A.cards[id];
      card.setAttribute('tabindex', '0');
      card.setAttribute('title', 'Показать динамику по дням');
      if (!card.querySelector('.kt-hint')) {
        var hint = document.createElement('div');
        hint.className = 'kt-hint';
        hint.textContent = 'динамика →';
        card.appendChild(hint);
      }
      if (getComputedStyle(card).position === 'static') card.style.position = 'relative';
    });

    var box = document.createElement('div');
    box.className = 'kpi-trend';
    box.id = 'kpi-trend';
    box.innerHTML = HTML;
    // Під рядком карток, але ПІСЛЯ підпису під ними, якщо він є: інакше
    // графік вклинювався б між числом і його поясненням.
    var after = row.nextElementSibling;
    var anchor = (after && after.classList.contains('kpi-note')) ? after : row;
    anchor.parentNode.insertBefore(box, anchor.nextSibling);
    box.querySelector('#kt-close').addEventListener('click', function () { current = null; render(); });

    document.addEventListener('click', function (e) {
      var card = e.target.closest ? e.target.closest('[data-metric]') : null;
      if (card) toggle(card.dataset.metric);
    });
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      var a = document.activeElement;
      var card = (a && a.closest) ? a.closest('[data-metric]') : null;
      if (card) { e.preventDefault(); toggle(card.dataset.metric); }
    });

    // Функцію перемальовування сторінки обгортаємо: вона викликається
    // щоразу, коли змінився період чи фільтр, і це єдиний надійний сигнал,
    // що графік показника теж час оновити.
    if (A.hook && typeof window[A.hook] === 'function') {
      var orig = window[A.hook];
      window[A.hook] = function () {
        var out = orig.apply(this, arguments);
        try { render(); } catch (err) { console.warn('[kpi-trend]', err); }
        return out;
      };
    }

    var t = null;
    window.addEventListener('resize', function () {
      if (!current) return;
      clearTimeout(t);
      t = setTimeout(draw, 150);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
