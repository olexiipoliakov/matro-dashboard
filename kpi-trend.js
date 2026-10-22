/* kpi-trend.js — динаміка показника під картками KPI на сторінці SMM.
 *
 * Клік по картці («Расход», «Переписок», «Цена за покупку»…) розкриває
 * лінійний графік саме цього числа по днях ОБРАНОГО періоду, з підказкою
 * «дата + значення» при наведенні.
 *
 * Чому окремий файл, а не код усередині index.html. Сторінка й так на
 * півтори тисячі рядків; усе, що стосується лише цього графіка, живе тут
 * і нічого в ній не ламає. В index.html від цієї фічі — один рядок
 * <script src="kpi-trend.js">.
 *
 * Файл спирається на глобальні змінні сторінки: FD (рядки день×кампанія,
 * уже відфільтровані за періодом), activeCamps (фільтр по кампанії),
 * fmt() (форматування чисел) і renderCharts() (її виклик — сигнал, що
 * дані перерахувались). Тому підключати його треба ПІСЛЯ основного
 * скрипта сторінки.
 */
(function () {
  'use strict';

  // ── показники ──────────────────────────────────────────────────────────
  // goals — цілі кампаній, які входять у показник. Ціна за листування
  // рахується ТІЛЬКИ по кампаніях на листування: якщо ділити витрати всіх
  // кампаній на результати однієї цілі, ціна завищується в рази.
  var METRICS = {
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
  };
  // Яку картку з чим звʼязати — за id числа всередині неї. Ці id на
  // сторінці стабільні, а тексти заголовків можуть змінитись.
  var CARDS = {
    'k-spend':'spend', 'k-cpl-conv':'conv_cpl', 'k-leads-conv':'conv_count',
    'k-cpl-purch':'purch_cpl', 'k-leads-purch':'purch_count'
  };

  var current = null;   // який показник розкритий
  var drawn = null;     // дані останнього графіка — для перемальовування

  var CSS = ''
    + '.kpi[data-metric]{cursor:pointer;transition:border-color 180ms ease,transform 180ms ease}'
    + '.kpi[data-metric]:hover{border-color:var(--border-strong);transform:translateY(-2px)}'
    + '.kpi[data-metric]:focus-visible{outline:2px solid var(--accent);outline-offset:2px}'
    + '.kpi.sel{border-color:rgba(255,140,66,.55)}'
    + '.kpi .hint{position:absolute;right:14px;bottom:12px;font-size:10.5px;color:var(--ink-dim);'
    + 'opacity:0;transition:opacity 180ms ease;pointer-events:none}'
    + '.kpi[data-metric]:hover .hint{opacity:.85}.kpi.sel .hint{opacity:.95;color:var(--accent)}'
    + '.kpi-trend{display:none;margin:-4px 0 24px}.kpi-trend.open{display:block}'
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
    + '.ln-tip{position:absolute;pointer-events:none;opacity:0;transition:opacity 120ms ease;'
    + 'background:#1a2033;border:1px solid var(--border-strong);border-radius:10px;padding:8px 11px;'
    + 'font-size:12px;line-height:1.45;white-space:nowrap;z-index:5;'
    + 'box-shadow:0 10px 24px rgba(0,0,0,.45);transform:translate(-50%,-115%)}'
    + '.ln-tip.on{opacity:1}.ln-tip .d{color:var(--ink-dim);font-size:11px;margin-bottom:2px}'
    + '.ln-tip .v{font-weight:700;font-variant-numeric:tabular-nums}'
    + '.kpi-trend .empty-note{padding:28px 4px;text-align:center;color:var(--ink-dim);font-size:13px}'
    + '.kpi-trend .legend .avg-note{color:var(--ink-dim)}';

  var HTML = ''
    + '<div class="panel">'
    + '  <div class="kpi-trend-head">'
    + '    <h3 id="kt-title">—</h3><span class="period" id="kt-period"></span>'
    + '    <button class="close" type="button" id="kt-close">Закрыть ×</button>'
    + '  </div>'
    + '  <div class="ln-wrap" id="kt-chart"></div>'
    + '  <div class="ln-xaxis" id="kt-xaxis"></div>'
    + '  <div class="legend" id="kt-legend"></div>'
    + '</div>';

  function money(v) {
    return (typeof fmt === 'function') ? fmt(v)
      : new Intl.NumberFormat('uk-UA').format(Math.round(v || 0));
  }

  // Групування точок: понад 31 стовпець/точку підписи дат зливаються,
  // тому довгі періоди зводимо по тижнях.
  function bucket(bydate) {
    var dates = Object.keys(bydate).sort();
    if (dates.length <= 31) {
      return {
        labels: dates.map(function (d) { return d.slice(8) + '.' + d.slice(5, 7); }),
        rows: dates.map(function (d) { return bydate[d]; }),
        starts: dates, weekly: false
      };
    }
    var wk = {};
    dates.forEach(function (d) {
      var dt = new Date(d + 'T00:00:00');
      var jan = new Date(dt.getFullYear(), 0, 1);
      var wn = Math.ceil((((dt - jan) / 86400000) + jan.getDay() + 1) / 7);
      var key = dt.getFullYear() + '-W' + String(wn).padStart(2, '0');
      if (!wk[key]) wk[key] = {spend: 0, leads: 0, first: d};
      wk[key].spend += bydate[d].spend;
      wk[key].leads += bydate[d].leads;
    });
    var keys = Object.keys(wk).sort();
    return {
      labels: keys.map(function (k) { var f = wk[k].first; return f.slice(8) + '.' + f.slice(5, 7); }),
      rows: keys.map(function (k) { return wk[k]; }),
      starts: keys.map(function (k) { return wk[k].first; }),
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
    if (!box) return;
    Array.prototype.forEach.call(document.querySelectorAll('.kpi[data-metric]'), function (el) {
      el.classList.toggle('sel', el.dataset.metric === current);
    });
    if (!current) { box.classList.remove('open'); drawn = null; return; }

    var m = METRICS[current];
    box.classList.add('open');
    document.getElementById('kt-title').textContent = m.title;

    // Та сама вибірка, що й у картках: фільтр дат + фільтр по кампанії.
    var rowsSrc = (typeof FD !== 'undefined' && FD) ? FD : [];
    if (typeof activeCamps !== 'undefined' && activeCamps && activeCamps.size > 0) {
      rowsSrc = rowsSrc.filter(function (r) { return activeCamps.has(r.campaign); });
    }
    if (m.goals) {
      rowsSrc = rowsSrc.filter(function (r) { return m.goals.indexOf(r.goal || 'Обращения') >= 0; });
    }

    var bydate = {};
    rowsSrc.forEach(function (r) {
      if (!bydate[r.date]) bydate[r.date] = {spend: 0, leads: 0};
      bydate[r.date].spend += r.spend || 0;
      bydate[r.date].leads += r.leads || 0;
    });

    var chart = document.getElementById('kt-chart');
    var legend = document.getElementById('kt-legend');
    var dates = Object.keys(bydate).sort();
    if (!dates.length) {
      chart.innerHTML = '<div class="empty-note">За выбранный период по этому показателю данных нет.</div>';
      document.getElementById('kt-period').textContent = '';
      document.getElementById('kt-xaxis').innerHTML = '';
      legend.style.display = 'none';
      drawn = null;
      return;
    }
    legend.style.display = '';

    var b = bucket(bydate);
    var values = b.rows.map(m.value);
    var fmtFn = function (v) { return money(Math.round(v)) + (m.unit ? ' ' + m.unit : ''); };

    var from = dates[0], to = dates[dates.length - 1];
    document.getElementById('kt-period').textContent =
      from.slice(8) + '.' + from.slice(5, 7) + ' — ' + to.slice(8) + '.' + to.slice(5, 7)
      + (b.weekly ? ' · по неделям' : ' · по дням')
      + ((typeof activeCamps !== 'undefined' && activeCamps && activeCamps.size > 0)
         ? ' · только выбранные кампании' : '');

    // Пунктир. Для ціни це ціна за весь період (те саме число, що в
    // картці), для кількостей — середнє за день. Середнє денних цін і ціна
    // за період — різні числа: дні з великою кількістю результатів важать
    // більше, і графік розходився б із карткою без пояснення.
    var totSpend = b.rows.reduce(function (a, r) { return a + r.spend; }, 0);
    var totLeads = b.rows.reduce(function (a, r) { return a + r.leads; }, 0);
    var avg = m.ratio ? (totLeads ? totSpend / totLeads : 0)
                      : (values.length ? values.reduce(function (a, v) { return a + v; }, 0) / values.length : 0);
    legend.innerHTML = '<span>Наведите на график, чтобы увидеть дату и значение.</span>'
      + '<span class="avg-note">Пунктир — '
      + (m.ratio ? 'цена за весь период' : 'среднее за день') + ': ' + fmtFn(avg) + '</span>';

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

    var style = document.createElement('style');
    style.textContent = CSS;
    document.head.appendChild(style);

    // Картки знаходимо за id числа всередині них.
    var kpis = null;
    Object.keys(CARDS).forEach(function (id) {
      var val = document.getElementById(id);
      var card = val && val.closest ? val.closest('.kpi') : null;
      if (!card) return;
      card.dataset.metric = CARDS[id];
      card.setAttribute('tabindex', '0');
      card.setAttribute('title', 'Показать динамику по дням');
      if (!card.querySelector('.hint')) {
        var hint = document.createElement('div');
        hint.className = 'hint';
        hint.textContent = 'динамика →';
        card.appendChild(hint);
      }
      if (!kpis) kpis = card.parentElement;
    });
    if (!kpis) return;   // сторінка змінилась — тихо нічого не робимо

    var box = document.createElement('div');
    box.className = 'kpi-trend';
    box.id = 'kpi-trend';
    box.innerHTML = HTML;
    kpis.parentNode.insertBefore(box, kpis.nextSibling);
    box.querySelector('#kt-close').addEventListener('click', function () { current = null; render(); });

    document.addEventListener('click', function (e) {
      var card = e.target.closest ? e.target.closest('.kpi[data-metric]') : null;
      if (card) toggle(card.dataset.metric);
    });
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      var a = document.activeElement;
      var card = (a && a.closest) ? a.closest('.kpi[data-metric]') : null;
      if (card) { e.preventDefault(); toggle(card.dataset.metric); }
    });

    // renderCharts() сторінка кличе щоразу, коли перерахувала дані під
    // новий період або фільтр. Чіпляємось до неї, щоб графік показника
    // завжди був про той самий відрізок часу, що й картка над ним.
    if (typeof window.renderCharts === 'function') {
      var orig = window.renderCharts;
      window.renderCharts = function () {
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
