/* gong.js — піксельний бігун і гонг на нове замовлення.
 *
 * Навіщо опитування, а не push. Bitrix шле вебхук серверу, але не нам:
 * у браузер він нічого не доставляє, та й сам вебхук каже лише «щось
 * змінилось», без самої угоди. Тому сервер за цим сигналом перезбирає
 * останні дні, а сторінка раз на 45 секунд питає короткий
 * /api/bitrix/pulse (кілька кілобайт замість 3,4 МБ файлу даних) і
 * порівнює id. Звідси затримка: від замовлення до гонга хвилина-дві.
 *
 * Що вважається новим — вирішує сторінка. Вона тримає список побачених
 * id у себе: так гонг не задзвонить удруге після перезапуску сервера й
 * не залежить від його часової зони.
 *
 * Звук. Браузер не дасть відтворити нічого, поки користувач не клікнув
 * по сторінці — це правило всюди. Тому в панелі є кнопка звуку, і саме
 * той клік розблоковує аудіо. Файли: sounds/order.mp3 (замовлення) і
 * sounds/lead.mp3 (гудок корабля на лід). Якщо файлів немає — синтез на
 * місці, щоб панель працювала й без них.
 *
 * Малювання. Канвас 320×96 розтягнутий на всю ширину з
 * image-rendering:pixelated — звідси великий чесний піксель, як на 16-бітній
 * консолі. Усе малюється прямокутниками по цілих координатах; жодної
 * картинки в репозиторії для цього не треба.
 */
(function () {
  'use strict';

  var KEY = 'matro-gong';
  var POLL_MS = 45000;
  var FILES = {order: 'sounds/order.mp3', lead: 'sounds/lead.mp3'};
  var VOL = {order: 0.85, lead: 0.5};

  // ── палітра ────────────────────────────────────────────────────────────
  var C = {
    sky1: '#0d1326', sky2: '#15204a', sky3: '#20306b',
    star: '#8fa6e8',
    hill: '#101a38', hill2: '#0b1226',
    sea:  '#122046',
    track: '#2a2039', trackTop: '#3a2d4e', lane: '#4a3b63',
    dirt: '#1a1430',
    skin: '#f0c08a', skinDark: '#c99a66',
    hair: '#2b1b12',
    shirt: '#FF8C42', shirtDark: '#c9661f',
    shorts: '#2d3f7d', shortsDark: '#1e2b57',
    shoe: '#f2f4ff', shoeDark: '#aab2d0',
    line: '#0a0718',
    gold: '#ffd24a', goldDark: '#c99a10',
    white: '#eef2ff',
    green: '#27E37A',
    ship: '#243a6b', shipLight: '#3f5b96', smoke: '#5a6a93'
  };

  var W = 400, H = 68, GROUND = 60;

  // ── стан ───────────────────────────────────────────────────────────────
  var on = false;              // звук увімкнено
  var seenDeals = null, seenLeads = null;
  var timer = null, raf = null;
  var actx = null, buffers = {};
  var cv = null, g = null;

  var scene = {
    t: 0,              // кадр
    scroll: 0,         // зсув землі
    mode: 'run',       // run | finish | win
    modeT: 0,
    finishX: 0,        // позиція фінішної стрічки
    runnerX: 64,
    confetti: [],
    ship: null,        // {x, dir, t}
    msg: '',
    score: 0
  };

  // ── звук ───────────────────────────────────────────────────────────────
  function ensureCtx() {
    var K = window.AudioContext || window.webkitAudioContext;
    if (!K) return null;
    if (!actx) actx = new K();
    if (actx.state === 'suspended') actx.resume();
    return actx;
  }

  function loadFile(key) {
    var ctx = ensureCtx();
    if (!ctx || buffers[key]) return;
    fetch(FILES[key])
      .then(function (r) {
        // Відсутній файл Flask віддає сторінкою, а не порожнечею, тому
        // дивимось і на тип: HTML у decodeAudioData полетить винятком.
        var t = r.headers.get('content-type') || '';
        if (!r.ok || t.indexOf('audio') < 0) throw new Error('no file');
        return r.arrayBuffer();
      })
      .then(function (a) { return ctx.decodeAudioData(a); })
      .then(function (b) { buffers[key] = b; })
      .catch(function () { /* лишаємось на синтезі */ });
  }

  // Запасний гудок корабля, якщо sounds/lead.mp3 не поклали: низький тон
  // з обертонами, повільна атака, довгий хвіст.
  function synthHorn(ctx) {
    var t0 = ctx.currentTime, out = ctx.createGain();
    out.gain.setValueAtTime(0.0001, t0);
    out.gain.exponentialRampToValueAtTime(0.5, t0 + 0.22);
    out.gain.setValueAtTime(0.5, t0 + 1.15);
    out.gain.exponentialRampToValueAtTime(0.0001, t0 + 2.1);
    out.connect(ctx.destination);
    [[1, 1], [2, 0.55], [3, 0.3], [4, 0.17], [6, 0.07]].forEach(function (p) {
      [92, 93.1].forEach(function (f, i) {
        var o = ctx.createOscillator(), gg = ctx.createGain();
        o.type = 'sine';
        o.frequency.value = f * p[0];
        gg.gain.value = p[1] * (i ? 0.7 : 1);
        o.connect(gg); gg.connect(out);
        o.start(t0); o.stop(t0 + 2.2);
      });
    });
  }

  function synthFanfare(ctx) {
    var t0 = ctx.currentTime;
    [[523, 0], [659, 0.13], [784, 0.26], [1047, 0.39]].forEach(function (p) {
      var o = ctx.createOscillator(), gg = ctx.createGain();
      o.type = 'square';
      o.frequency.value = p[0];
      gg.gain.setValueAtTime(0.0001, t0 + p[1]);
      gg.gain.exponentialRampToValueAtTime(0.16, t0 + p[1] + 0.02);
      gg.gain.exponentialRampToValueAtTime(0.0001, t0 + p[1] + 0.34);
      o.connect(gg); gg.connect(ctx.destination);
      o.start(t0 + p[1]); o.stop(t0 + p[1] + 0.4);
    });
  }

  function play(key) {
    if (!on) return;
    var ctx = ensureCtx();
    if (!ctx) return;
    if (buffers[key]) {
      var s = ctx.createBufferSource();
      s.buffer = buffers[key];
      var gg = ctx.createGain();
      gg.gain.value = VOL[key];
      s.connect(gg); gg.connect(ctx.destination);
      s.start();
      return;
    }
    if (key === 'lead') synthHorn(ctx); else synthFanfare(ctx);
  }

  // ── малювання ──────────────────────────────────────────────────────────
  function px(x, y, w, h, c) { g.fillStyle = c; g.fillRect(x | 0, y | 0, w | 0, h | 0); }

  // Зірки стоять на місці — масив рахуємо один раз, інакше вони миготіли б
  // випадковими точками щокадру.
  var STARS = (function () {
    var out = [], seed = 7;
    var rnd = function () { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed / 0x7fffffff; };
    for (var i = 0; i < 30; i++) out.push([Math.floor(rnd() * 400), Math.floor(rnd() * 24), rnd() > 0.7 ? 2 : 1]);
    return out;
  })();

  function drawSky() {
    px(0, 0, W, 20, C.sky1);
    px(0, 20, W, 10, C.sky2);
    px(0, 30, W, 8, C.sky3);
    // Дизеринг на стику смуг — щоб перехід не був лінійкою.
    for (var x = 0; x < W; x += 2) {
      px(x + ((x / 2) % 2), 19, 1, 1, C.sky2);
      px(x + ((x / 2) % 2), 29, 1, 1, C.sky3);
    }
    STARS.forEach(function (s) { px(s[0], s[1], s[2], s[2], C.star); });
  }

  // Пагорби — не окремі фігури, а суцільний гребінь: для кожного стовпця
  // рахуємо висоту сумою двох синусів. Так шар змикається сам із собою на
  // будь-якому зсуві, і між «горами» не лишається провалів до неба.
  function ridge(off, speed, baseY, amp, period, col, topCol) {
    for (var x = 0; x < W; x++) {
      var u = (x + off * speed) / period;
      var h = amp * (0.55 * Math.sin(u) + 0.45 * Math.sin(u * 2.3 + 1.7));
      var y = Math.round(baseY - amp * 0.5 - h);
      px(x, y, 1, baseY - y, col);
      px(x, y, 1, 1, topCol);
    }
  }

  function drawHills(off) {
    ridge(off, 0.10, 40, 8, 24, C.hill2, '#18264d');
    ridge(off, 0.22, 43, 6, 16, C.hill, '#1b2c58');
    px(0, 42, W, 2, C.sea);
    // Відблиски на воді — коротенькі рисочки, що повзуть повільніше за землю.
    for (var i = 0; i < 9; i++) {
      var sx = ((i * 47 - Math.floor(off * 0.3)) % (W + 94) + W + 94) % (W + 94) - 47;
      px(sx, 43, 5, 1, '#1d3160');
    }
  }

  function drawTrack(off) {
    px(0, 44, W, 3, C.dirt);
    px(0, 47, W, 2, C.trackTop);
    px(0, 49, W, H - 49, C.track);
    px(0, 46, W, 1, C.line);
    // Смуги доріжки їдуть назустріч бігунові — це і є відчуття швидкості.
    for (var i = 0; i < 18; i++) {
      var x = ((i * 26 - Math.floor(off)) % (W + 52) + W + 52) % (W + 52) - 26;
      px(x, 54, 13, 2, C.lane);
      px(x + 6, 64, 9, 2, C.lane);
    }
    // Придорожні стовпчики — без них швидкість видно тільки по смугах.
    for (var k = 0; k < 7; k++) {
      var cx = ((k * 61 - Math.floor(off * 1.25)) % (W + 122) + W + 122) % (W + 122) - 61;
      px(cx, 44, 1, 4, '#3b4f8a');
      px(cx, 43, 2, 1, C.gold);
    }
  }

  // Бігун. Тулуб і голова намальовані як є, руки й ноги — по кадрах:
  // шість положень дають цикл, у якому видно і винос, і проводку.
  // Ноги й руки по кадрах. Координати — від лінії стегон і від плеча,
  // у пікселях. Кожна нога це стегно, гомілка і стопа; рука — плече й
  // передпліччя, зігнуте в лікті: пряма горизонтальна рука читалась би
  // як жердина в руках, а не як біг.
  //
  // Кадрів шість, але таблиці на три: друга половина циклу — та сама
  // трійка з переставленими ногами. Так дві половини не можуть
  // розійтися між собою при правці.
  function mirror(a) { return a.concat(a.map(function (p) { return [p[1], p[0]]; })); }

  //                 стегно          гомілка         стопа
  var LEGS = mirror([
    [[[1, 0, 3, 4], [3, 4, 3, 4], [4, 8, 5, 2]],      // попереду
     [[-2, 0, 3, 4], [-5, 4, 3, 4], [-8, 8, 5, 2]]],  // позаду
    [[[0, 0, 3, 4], [1, 4, 3, 5], [1, 9, 5, 2]],      // опора
     [[-1, 0, 3, 3], [-4, 3, 3, 4], [-6, 7, 4, 2]]],  // підтягується
    [[[-2, 0, 3, 4], [-4, 4, 3, 4], [-7, 8, 5, 2]],   // відштовхнулась
     [[1, 0, 3, 3], [2, 3, 3, 4], [3, 7, 5, 2]]]      // виноситься
  ]);

  //                 плече           передпліччя
  var ARMS = mirror([
    [[[1, 0, 2, 3], [3, 2, 3, 3]],                    // вперед
     [[-3, 0, 2, 3], [-5, 2, 3, 3]]],                 // назад
    [[[0, 0, 2, 4], [0, 4, 2, 3]],                    // вниз
     [[-2, 0, 2, 3], [-4, 3, 3, 2]]],
    [[[-2, 0, 2, 3], [-4, 2, 3, 3]],
     [[1, 0, 2, 3], [2, 3, 3, 3]]]
  ]);

  // bx — центр бігуна, ноги стоять на GROUND.
  function drawRunner(bx, frame, pose) {
    var f = frame % 6;
    var feet = GROUND;
    var hy = feet - 11;        // лінія стегон
    var ty = hy - 10;          // верх тулуба (плечі)
    var head = ty - 7;         // верх голови
    var lean = pose === 'win' ? 0 : 2;   // нахил уперед — без нього це хода
    var bob = pose === 'win' ? 0 : (f === 1 || f === 4 ? 1 : 0);
    hy -= bob; ty -= bob; head -= bob;

    // дальня рука й дальня нога — темнішим, щоб читалась глибина
    function limb(set, ox, oy, far) {
      set.forEach(function (seg, i) {
        var col = far ? C.skinDark : C.skin;
        if (i === 2) col = far ? C.shoeDark : C.shoe;
        px(ox + seg[0], oy + seg[1], seg[2], seg[3], col);
      });
    }

    if (pose !== 'win') {
      limb(LEGS[f][1], bx, hy, true);
      limb(ARMS[f][1], bx + lean, ty + 2, true);
    }

    // тінь під бігуном
    px(bx - 6, feet + 1, 13, 1, '#15102a');

    if (pose === 'win') {
      // Кубок над головою, руки знизу до його підставки. Висота кубка
      // рахується від голови, а не від землі: інакше підстрибування
      // переможця від'їжджало б окремо від рук.
      var up = (Math.floor(frame / 9) % 2) ? 0 : 1;
      var cy = head - 17 + up;
      px(bx - 6, cy + 1, 2, 3, C.goldDark);          // вушка
      px(bx + 5, cy + 1, 2, 3, C.goldDark);
      px(bx - 4, cy, 9, 2, C.gold);                  // вінце
      px(bx - 3, cy + 2, 7, 3, C.gold);              // чаша
      px(bx - 2, cy + 5, 5, 1, C.goldDark);
      px(bx - 1, cy + 6, 3, 2, C.gold);              // ніжка
      px(bx - 3, cy + 8, 7, 2, C.gold);              // підставка
      px(bx - 2, cy + 1, 2, 1, '#fff6c9');           // відблиск
      // руки від плечей до підставки
      var armTop = cy + 8, armH = (ty + 4) - armTop;
      px(bx - 5, armTop, 2, armH, C.skinDark);
      px(bx + 4, armTop, 2, armH, C.skin);
      // іскри навколо кубка
      if (Math.floor(frame / 6) % 2) {
        px(bx - 8, cy - 2, 1, 1, '#fff6c9');
        px(bx + 8, cy + 3, 1, 1, '#fff6c9');
      } else {
        px(bx + 8, cy - 3, 1, 1, '#fff6c9');
        px(bx - 9, cy + 4, 1, 1, '#fff6c9');
      }
    }

    // голова
    px(bx - 3, head, 7, 2, C.hair);
    px(bx - 4, head + 2, 8, 2, C.hair);
    px(bx - 3, head + 4, 6, 4, C.skin);
    px(bx + 2, head + 5, 1, 1, C.line);
    px(bx - 4, head + 4, 2, 3, C.hair);
    px(bx + 3, head + 6, 1, 1, C.skinDark);

    // тулуб
    px(bx - 4 + lean, ty, 8, 7, C.shirt);
    px(bx - 4 + lean, ty + 5, 8, 2, C.shirtDark);
    px(bx - 1 + lean, ty + 1, 1, 4, '#ffb070');
    px(bx - 3 + lean, ty + 7, 7, 4, C.shorts);
    px(bx - 3 + lean, ty + 10, 7, 1, C.shortsDark);

    if (pose === 'win') {
      px(bx - 3, hy, 3, 9, C.skinDark);
      px(bx + 1, hy, 3, 9, C.skin);
      px(bx - 5, hy + 9, 5, 2, C.shoeDark);
      px(bx + 1, hy + 9, 5, 2, C.shoe);
    } else {
      limb(LEGS[f][0], bx, hy, false);
      limb(ARMS[f][0], bx + lean, ty + 2, false);
    }
  }

  function drawFinish(x) {
    if (x < -30 || x > W + 30) return;
    px(x, 20, 2, GROUND - 18, C.white);          // стовп
    px(x - 1, GROUND + 2, 4, 2, C.shoeDark);
    // клітчаста стрічка — невелика, щоб не закривала бігуна
    for (var r = 0; r < 3; r++) {
      for (var c = 0; c < 7; c++) {
        px(x + 2 + c * 3, 20 + r * 3, 3, 3, ((r + c) % 2) ? C.line : C.white);
      }
    }
    px(x + 2, 29, 21, 1, C.gold);
    // стрічка впоперек доріжки — те, що бігун рве грудьми
    px(x + 1, GROUND - 10, 1, 1, C.gold);
    for (var i = 0; i < 10; i++) px(x + 2 + i * 2, GROUND - 10, 1, 1, C.gold);
  }

  function drawShip(s) {
    // Корабель іде по лінії води (вона на 42), а не по доріжці — після
    // того, як сцену стиснули по висоті, стара координата опинилась би
    // просто посеред треку.
    var x = s.x | 0, y = 38;
    px(x + 2, y, 14, 3, C.ship);               // корпус
    px(x, y + 3, 18, 2, C.shipLight);
    px(x + 5, y - 4, 2, 4, C.ship);            // труба
    px(x + 5, y - 5, 2, 1, C.gold);
    px(x + 9, y - 3, 6, 3, C.ship);            // надбудова
    for (var i = 0; i < 3; i++) {
      px(x + 4 - i, y - 8 - i * 3, 2 + i, 2, C.smoke);   // дим
    }
    px(x, y + 5, 18, 1, C.sea);
  }

  function drawConfetti() {
    scene.confetti.forEach(function (p) {
      px(p.x, p.y, p.s, p.s, p.c);
    });
  }

  function draw() {
    if (!g) return;
    drawSky();
    if (scene.ship) drawShip(scene.ship);
    drawHills(scene.scroll);
    drawTrack(scene.scroll);
    if (scene.mode !== 'run') drawFinish(scene.finishX);
    drawRunner(scene.runnerX, Math.floor(scene.t / 4), scene.mode === 'win' ? 'win' : 'run');
    if (scene.mode === 'win') drawConfetti();
  }

  // ── цикл ───────────────────────────────────────────────────────────────
  var CONF_COLORS = ['#FF8C42', '#27E37A', '#32D8FF', '#ffd24a', '#FF5E6C', '#eef2ff'];

  function step() {
    scene.t++;
    var speed = scene.mode === 'win' ? 0 : 2.6;
    scene.scroll += speed;

    if (scene.ship) {
      scene.ship.x += 0.35;
      if (scene.ship.x > W + 24) scene.ship = null;
    }

    if (scene.mode === 'finish') {
      scene.finishX -= 3.4;
      if (scene.finishX <= scene.runnerX) {
        scene.mode = 'win';
        scene.modeT = 0;
        for (var i = 0; i < 54; i++) {
          scene.confetti.push({
            x: 10 + Math.random() * (W - 20), y: -4 - Math.random() * 46,
            vy: 0.55 + Math.random() * 1.3, vx: (Math.random() - 0.5) * 0.8,
            s: Math.random() > 0.6 ? 3 : 2,
            c: CONF_COLORS[(Math.random() * CONF_COLORS.length) | 0]
          });
        }
      }
    } else if (scene.mode === 'win') {
      scene.modeT++;
      // Стрічка лишається позаду переможця: так видно, що він її перетнув,
      // а не стоїть перед нею.
      if (scene.finishX > scene.runnerX - 34) scene.finishX -= 1.1;
      scene.confetti.forEach(function (p) { p.y += p.vy; p.x += p.vx; });
      scene.confetti = scene.confetti.filter(function (p) { return p.y < H + 4; });
      if (scene.modeT > 9 * 60) {                 // ~9 секунд свята
        scene.mode = 'run';
        scene.confetti = [];
        setMsg('');
      }
    }
    draw();
  }

  function loop() {
    raf = requestAnimationFrame(loop);
    if (document.hidden) return;
    step();
  }

  // ── події ──────────────────────────────────────────────────────────────
  function setMsg(text, cls) {
    var el = document.getElementById('run-msg');
    if (!el) return;
    el.textContent = text || '';
    el.className = 'run-msg' + (text ? ' show' : '') + (cls ? ' ' + cls : '');
  }

  function money(v) {
    return (typeof fmt === 'function') ? fmt(v)
      : new Intl.NumberFormat('uk-UA').format(Math.round(v || 0));
  }

  function chanName(src) {
    var api = window.MATRO_CHAN;
    return (api && api.label) ? api.label(src) : '';
  }

  function onOrder(n, sum, src) {
    scene.mode = 'finish';
    scene.finishX = W + 12;
    scene.confetti = [];
    var word = (n === 1) ? 'НОВЕ ЗАМОВЛЕННЯ'
             : (n >= 2 && n <= 4) ? n + ' НОВІ ЗАМОВЛЕННЯ'
             : n + ' НОВИХ ЗАМОВЛЕНЬ';
    var parts = [word, money(sum) + ' ₴'];
    var ch = (n === 1) ? chanName(src) : '';
    if (ch) parts.push(ch.toUpperCase());
    setMsg(parts.join('  ·  '), 'win');
    play('order');
  }

  function onLead() {
    if (!scene.ship) scene.ship = {x: -24};
    setMsg('НОВИЙ ЛІД', 'lead');
    setTimeout(function () {
      var el = document.getElementById('run-msg');
      if (el && el.classList.contains('lead')) setMsg('');
    }, 4000);
    play('lead');
  }

  // ── опитування ─────────────────────────────────────────────────────────
  function tick() {
    fetch('/api/bitrix/pulse?_=' + Date.now(), {credentials: 'same-origin'})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || !d.ok) return;
        var deals = d.deals || [], leads = d.leads || [];
        // Рахунок у кутку — замовлення найсвіжішого дня у файлі. Саме дня
        // з даних, а не «сьогодні» за годинником: о пів на першу ночі
        // лічильник інакше обнулявся б раніше, ніж приходять дані.
        var last = '';
        deals.forEach(function (x) { if (x[3] > last) last = x[3]; });
        scene.score = deals.filter(function (x) { return x[3] === last; }).length;
        paintScore();

        if (seenDeals === null) {
          // Перший запит лише запам'ятовує, що вже було: інакше вмикання
          // о шостій вечора дало б свято на всі сьогоднішні угоди.
          seenDeals = {}; seenLeads = {};
          deals.forEach(function (x) { seenDeals[x[0]] = 1; });
          leads.forEach(function (id) { seenLeads[id] = 1; });
          return;
        }
        var fresh = deals.filter(function (x) { return !seenDeals[x[0]]; });
        var newLeads = leads.filter(function (id) { return !seenLeads[id]; });
        deals.forEach(function (x) { seenDeals[x[0]] = 1; });
        leads.forEach(function (id) { seenLeads[id] = 1; });

        if (fresh.length) {
          onOrder(fresh.length, fresh.reduce(function (a, x) { return a + (x[1] || 0); }, 0),
                  fresh[0][2]);
        } else if (newLeads.length) {
          onLead();   // угода важливіша: якщо прийшло і те, і те, грає свято
        }
      })
      .catch(function () { /* мережа моргнула — спробуємо наступного разу */ });
  }

  function paintScore() {
    var el = document.getElementById('run-score');
    if (el) el.textContent = 'ЗАМОВЛЕНЬ: ' + scene.score;
  }

  // ── вигляд ─────────────────────────────────────────────────────────────
  var CSS = ''
    + '.run-panel{position:relative;margin:0 0 18px;border:1px solid var(--border);'
    + 'border-radius:16px;overflow:hidden;background:#0d1326;'
    + 'box-shadow:0 10px 30px rgba(0,0,0,.35)}'
    + '.run-panel canvas{display:block;width:100%;height:auto;'
    + 'image-rendering:pixelated;image-rendering:crisp-edges}'
    + '.run-top{position:absolute;left:0;right:0;top:0;display:flex;align-items:center;'
    + 'gap:10px;padding:9px 12px;pointer-events:none}'
    + '.run-score{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:10px;'
    + 'letter-spacing:1.4px;color:#8fa6e8;text-shadow:0 1px 0 #000}'
    + '.run-snd{margin-left:auto;pointer-events:auto;cursor:pointer;font-family:ui-monospace,'
    + 'SFMono-Regular,Menlo,monospace;font-size:10px;letter-spacing:1.2px;'
    + 'color:#8fa6e8;background:rgba(0,0,0,.35);border:1px solid rgba(143,166,232,.35);'
    + 'border-radius:7px;padding:5px 9px;transition:all 150ms ease}'
    + '.run-snd:hover{color:#eef2ff;border-color:rgba(238,242,255,.6)}'
    + '.run-snd.on{color:#27E37A;border-color:rgba(39,227,122,.55);background:rgba(39,227,122,.12)}'
    + '.run-msg{position:absolute;left:50%;bottom:10px;transform:translate(-50%,16px);'
    + 'font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;font-weight:700;'
    + 'letter-spacing:1.6px;white-space:nowrap;padding:7px 14px;border-radius:9px;'
    + 'background:rgba(5,8,18,.82);border:1px solid rgba(255,255,255,.14);'
    + 'color:#eef2ff;opacity:0;pointer-events:none;'
    + 'transition:opacity 220ms ease,transform 380ms cubic-bezier(.17,.89,.32,1.28)}'
    + '.run-msg.show{opacity:1;transform:translate(-50%,0)}'
    + '.run-msg.win{border-color:rgba(255,210,74,.6);color:#ffd24a;'
    + 'box-shadow:0 0 26px rgba(255,210,74,.22)}'
    + '.run-msg.lead{border-color:rgba(50,216,255,.5);color:#32D8FF}'
    + '@media(max-width:560px){.run-msg{font-size:9px;letter-spacing:1px;padding:5px 9px}}';

  function init() {
    var bar = document.querySelector('.kpi-bar');
    if (!bar) return;   // не головна — тихо нічого не робимо

    var style = document.createElement('style');
    style.textContent = CSS;
    document.head.appendChild(style);

    var panel = document.createElement('div');
    panel.className = 'run-panel';
    panel.innerHTML = ''
      + '<canvas id="run-cv" width="' + W + '" height="' + H + '"></canvas>'
      + '<div class="run-top"><span class="run-score" id="run-score">ЗАМОВЛЕНЬ: 0</span>'
      + '<button class="run-snd" id="run-snd" type="button">ЗВУК OFF</button></div>'
      + '<div class="run-msg" id="run-msg"></div>';
    bar.parentNode.insertBefore(panel, bar);

    cv = document.getElementById('run-cv');
    g = cv.getContext('2d');
    g.imageSmoothingEnabled = false;

    document.getElementById('run-snd').addEventListener('click', function () {
      setSound(!on, true);
    });

    // Після перезавантаження звук заблокований знову, а стан відновився з
    // пам'яті. Ловимо перший-ліпший клік і доганяємо те, що мав зробити
    // клік по кнопці.
    var unlocked = false;
    var unlock = function () {
      if (unlocked || !on) return;
      unlocked = true;
      ensureCtx(); loadFile('order'); loadFile('lead');
    };
    document.addEventListener('pointerdown', unlock, true);
    document.addEventListener('keydown', unlock, true);

    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) tick();
    });

    var saved = '0';
    try { saved = localStorage.getItem(KEY) || '0'; } catch (e) {}
    setSound(saved === '1', false);

    draw();
    loop();
    tick();
    timer = setInterval(function () { if (!document.hidden) tick(); }, POLL_MS);
  }

  function setSound(v, byUser) {
    on = !!v;
    try { localStorage.setItem(KEY, on ? '1' : '0'); } catch (e) {}
    var b = document.getElementById('run-snd');
    if (b) {
      b.classList.toggle('on', on);
      b.textContent = on ? 'ЗВУК ON' : 'ЗВУК OFF';
      b.title = on ? 'Звук замовлень і лідів увімкнено. Працює, поки вкладка відкрита'
                   : 'Увімкнути звук замовлень і лідів';
    }
    if (on) {
      ensureCtx();
      if (byUser) {
        // Клік — єдиний момент, коли браузер дозволяє і завантажити, і
        // програти звук. Заразом даємо почути, що він увімкнувся.
        loadFile('order'); loadFile('lead');
        setTimeout(function () { play('lead'); }, 120);
      }
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
