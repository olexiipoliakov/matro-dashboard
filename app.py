"""
app.py — веб-сервер для розгортання matro-dashboard на Render.com замість
GitHub Pages. Робить три речі:

1. Віддає ті самі HTML/JSON файли дашборду, що й раніше — але тепер за
   логіном і паролем (своя сторінка /login), бо сервер більше не публічний
   статичний сайт.
2. Приймає вихідний вебхук з Bitrix24 (/webhook/bitrix) — при кожній зміні
   угоди/ліда Bitrix сам стукається сюди, і ми одразу перезапускаємо
   fetch_bitrix.py, замість того щоб чекати наступного розкладу.
3. Сам кличе fetch_meta.py / fetch_gsc.py / fetch_bitrix.py за розкладом
   (раз на 3 години) — так само, як раніше це робив GitHub Actions,
   просто тепер планувальник крутиться прямо тут, у самому сервері.
"""
import os, sys, time, json, hmac, hashlib, re, threading, subprocess, urllib.parse
from datetime import datetime
from pathlib import Path
from functools import wraps

import requests
from flask import Flask, request, send_from_directory, Response, jsonify, redirect
from itsdangerous import URLSafeTimedSerializer
from apscheduler.schedulers.background import BackgroundScheduler

BASE_DIR = Path(__file__).parent
app = Flask(__name__, static_folder=None)

# ── Вхід на дашборд: логін, пароль і сесія в cookie ──────────────────────
#
# Раніше тут був Basic Auth — те саме вікно, яке браузер малює сам. У нього
# дві вади, через які й знадобилась ця переробка:
#
#   1. З нього неможливо вийти й неможливо когось відключити. Браузер
#      запам'ятовує логін з паролем і підставляє їх у кожен наступний
#      запит — поки людина сама не почистить налаштування. Відкликати
#      доступ у того, кому колись дали пароль, не вийшло б.
#   2. Виглядає як системна помилка, а не як вхід до дашборду.
#
# Тепер: власна сторінка /login, а після входу — підписана cookie на 30 днів.
#
# ЯК ЗАКРИТИ ДОСТУП УСІМ ОДРАЗУ. Ключ, яким підписані cookie, виводиться з
# самого пароля (див. _signer нижче). Тому достатньо змінити
# DASHBOARD_PASSWORD у змінних середовища на Render — і всі раніше видані
# cookie миттєво стають недійсними: перевірку підпису новим ключем вони не
# проходять. Якщо пароль міняти не хочеться, а розлогінити всіх треба —
# постав або зміни AUTH_EPOCH (будь-який рядок: 2, 3, 2026-11-05).
DASH_USER = os.environ.get("DASHBOARD_USER", "admin").strip()
DASH_PASS = os.environ.get("DASHBOARD_PASSWORD", "")
AUTH_EPOCH = os.environ.get("AUTH_EPOCH", "1").strip()

# Окремий логін тільки для сторінки озвучки (/voice/…): актор — людина не з
# команди, і пароль від усього дашборду, де CRM і суми, йому давати не можна.
# Якщо VOICE_PASSWORD не заданий, сторінка озвучки просто вимагає звичайний
# логін, як і решта дашборду.
VOICE_USER = os.environ.get("VOICE_USER", "voice").strip()
VOICE_PASS = os.environ.get("VOICE_PASSWORD", "")

COOKIE_NAME = "matro_session"
SESSION_DAYS = 30

# Підпис cookie. Окрему змінну під секрет не заводимо: ключ — похідна від
# пароля, тому зміна пароля автоматично означає «розлогінити всіх». Сам
# пароль у cookie не потрапляє, тільки підпис, з якого його не відновити.
_signer = None
if DASH_PASS or VOICE_PASS:
    _signer = URLSafeTimedSerializer(
        hashlib.sha256(f"matro-dash|{AUTH_EPOCH}|{DASH_PASS}|{VOICE_PASS}".encode()).hexdigest(),
        salt="dashboard-login",
    )


def _eq(a, b):
    # compare_digest на рядках падає, якщо в паролі є кирилиця — порівнюємо
    # байти. Порівняння постійного часу: щоб пароль не можна було підібрати
    # по символах, вимірюючи час відповіді.
    return hmac.compare_digest((a or "").encode("utf-8"), (b or "").encode("utf-8"))


def check_login(username, password):
    """Яку частину дашборду відкриває ця пара: "all", "voice" або None."""
    u = (username or "").strip()
    if DASH_PASS and _eq(u, DASH_USER) and _eq(password, DASH_PASS):
        return "all"
    if VOICE_PASS and _eq(u, VOICE_USER) and _eq(password, VOICE_PASS):
        return "voice"
    return None


def current_scope():
    """Що дозволяє cookie цього запиту. None — не залогінений."""
    token = request.cookies.get(COOKIE_NAME, "")
    if not token or not _signer:
        return None
    try:
        data = _signer.loads(token, max_age=SESSION_DAYS * 86400)
    except Exception:
        # Прострочена, підроблена або підписана старим паролем — все одно
        # «не залогінений», різниці для нас немає.
        return None
    return data.get("s") if isinstance(data, dict) else None


def _is_navigation():
    """Людина відкриває сторінку — чи це фоновий fetch() зі сторінки.

    Переадресовувати на /login можна тільки перше: якщо відповісти
    редиректом на запит data.json, сторінка отримає HTML замість JSON і
    впаде з незрозумілою помилкою замість того, щоб показати вхід.
    """
    mode = request.headers.get("Sec-Fetch-Mode", "")
    if mode:
        return mode == "navigate"
    return "text/html" in request.headers.get("Accept", "")


def _deny():
    if _is_navigation():
        nxt = request.full_path if request.query_string else request.path
        return redirect("/login?next=" + urllib.parse.quote(nxt, safe=""), code=302)
    return Response(json.dumps({"error": "auth_required"}), 401,
                    {"Content-Type": "application/json; charset=utf-8"})


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not DASH_PASS:
            # Пароль не заданий у змінних середовища — навмисно НЕ пускаємо
            # нікого, щоб не залишити дашборд відкритим "по замовчуванню".
            return Response("DASHBOARD_PASSWORD не задано на сервері.", 500)
        if current_scope() == "all":
            return f(*args, **kwargs)
        return _deny()
    return decorated

# ── Фонові прогони fetch-скриптів ────────────────────────────────────────
# Остання доля кожного скрипта: {назва: {ok, code, finished_at, note}}.
# Без цього єдиним способом дізнатись, чому немає даних, були логи Render —
# а туди не завжди є доступ у того, хто дивиться на дашборд.
LAST_RUN = {}


def run_script(name, timeout=1200):
    print(f"[scheduler] запускаю {name}…", flush=True)
    started = time.time()
    try:
        result = subprocess.run([sys.executable, str(BASE_DIR / name)], check=False, timeout=timeout)
        # Раніше тут писали "завершено" незалежно від коду завершення —
        # якщо скрипт падав з необробленим винятком (traceback), лог все
        # одно виглядав так, ніби все пройшло успішно, і це маскувало
        # реальні збої (саме так довго непомітно ламався fetch_meta.py).
        LAST_RUN[name] = {"ok": result.returncode == 0, "code": result.returncode,
                          "finished_at": time.time(), "seconds": round(time.time() - started),
                          "note": ""}
        if result.returncode == 0:
            print(f"[scheduler] {name} завершено", flush=True)
        else:
            print(f"[scheduler] ✗ {name} ЗАВЕРШИВСЯ З ПОМИЛКОЮ (код {result.returncode}) — дивись traceback вище", flush=True)
    except Exception as e:
        LAST_RUN[name] = {"ok": False, "code": None, "finished_at": time.time(),
                          "seconds": round(time.time() - started), "note": str(e)[:300]}
        print(f"[scheduler] {name} впав: {e}", flush=True)

# Збір розділено на два розклади, бо джерела живуть із різною швидкістю.
# CRM і дзвінки міняються протягом дня — угоду оформили о другій, і о другій
# же її хочуть побачити. Реклама й Search Console оновлюються на стороні
# Google і Meta раз на кілька годин, тягати їх частіше немає сенсу: це довгі
# запити, які на 0.5 CPU займають сервер на хвилини.
def run_fast_periodic():
    run_script("fetch_bitrix.py")
    run_script("fetch_ringostat.py")


def run_slow_periodic():
    run_script("fetch_meta.py")
    run_script("fetch_gsc.py")

# ── Вебхук з Bitrix — миттєве оновлення при зміні угоди/ліда ────────────
WEBHOOK_SECRET = os.environ.get("BITRIX_INCOMING_WEBHOOK_SECRET", "")
_last_bitrix_trigger = 0.0
_bitrix_lock = threading.Lock()

def trigger_bitrix_refresh():
    """Дебаунс на 60с — якщо в Bitrix одразу змінили кілька угод підряд
    (типова ситуація для масового імпорту), не запускаємо fetch_bitrix.py
    на кожну подію окремо, а лише раз на хвилину максимум."""
    global _last_bitrix_trigger
    with _bitrix_lock:
        now = time.time()
        if now - _last_bitrix_trigger < 60:
            return
        _last_bitrix_trigger = now
    threading.Thread(target=lambda: run_script("fetch_bitrix.py"), daemon=True).start()

# Ручне оновлення CRM кнопкою «Оновити дані» на сторінці Bitrix. Дані
# збираються раз на добу, а менеджеру часом треба побачити угоду, оформлену
# десять хвилин тому, — без цього він дивиться на вчорашній зріз і не розуміє,
# чому його продажу немає.
_bitrix_running = False
_bitrix_run_lock = threading.Lock()


def _bitrix_worker():
    global _bitrix_running
    try:
        run_script("fetch_bitrix.py", timeout=1800)
    finally:
        with _bitrix_run_lock:
            _bitrix_running = False


def start_bitrix_refresh():
    """True — запустили, False — вже виконується."""
    global _bitrix_running
    with _bitrix_run_lock:
        if _bitrix_running:
            return False
        _bitrix_running = True
    threading.Thread(target=_bitrix_worker, daemon=True).start()
    return True


@app.route("/api/bitrix/refresh", methods=["POST"])
@requires_auth
def bitrix_refresh():
    if not start_bitrix_refresh():
        return jsonify({"status": "already_running"}), 409
    return jsonify({"status": "started"}), 202


@app.route("/api/bitrix/refresh/status")
@requires_auth
def bitrix_refresh_status():
    # Чи живий процес — знає тільки сервер; час зрізу читаємо з самого файлу,
    # щоб сторінка могла зрозуміти, що дані вже змінились, і перечитати їх.
    stamp = ""
    f = BASE_DIR / "bitrix_data.json"
    if f.exists():
        try:
            stamp = json.loads(f.read_text(encoding="utf-8")).get("generated_at", "")
        except Exception:
            stamp = ""
    with _bitrix_run_lock:
        running = _bitrix_running
    return jsonify({"running": running, "generated_at": stamp})


@app.route("/webhook/bitrix", methods=["POST"])
def bitrix_webhook():
    # Bitrix надсилає application/x-www-form-urlencoded з полем
    # auth[application_token] — саме той токен, який Bitrix видає при
    # створенні вихідного вебхука. Звіряємо, щоб цей ендпоінт не смикнув
    # хтось сторонній.
    token = request.form.get("auth[application_token]") or request.values.get("auth[application_token]", "")
    if WEBHOOK_SECRET and token != WEBHOOK_SECRET:
        return "forbidden", 403
    trigger_bitrix_refresh()
    return "ok", 200

# ── Сторінка входу ───────────────────────────────────────────────────────
#
# Захист від перебору пароля. Пароль один на всіх, тобто вгадувати потрібно
# рівно один рядок — без обмеження спроб його можна спокійно перебирати
# скриптом. 10 невдач з однієї адреси за 15 хвилин, далі ця адреса чекає.
_LOGIN_FAILS = {}            # ip → (кількість невдач, час першої з них)
_LOGIN_FAILS_LOCK = threading.Lock()
FAIL_LIMIT = 10
FAIL_WINDOW = 15 * 60


def _client_ip():
    # Render стоїть за своїм проксі, тому request.remote_addr — це адреса
    # проксі, однакова для всіх. Справжня адреса — перша в X-Forwarded-For.
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or "?"


def _throttled(ip):
    with _LOGIN_FAILS_LOCK:
        n, since = _LOGIN_FAILS.get(ip, (0, 0.0))
        return n >= FAIL_LIMIT and (time.time() - since) <= FAIL_WINDOW


def _note_fail(ip):
    with _LOGIN_FAILS_LOCK:
        n, since = _LOGIN_FAILS.get(ip, (0, 0.0))
        if time.time() - since > FAIL_WINDOW:
            n, since = 0, time.time()
        _LOGIN_FAILS[ip] = (n + 1, since)


def _safe_next(value):
    """Куда вести после входа. Только свой путь: с ?next=https://… чужой
    сайт міг би отримати посилання на вхід, яке веде від нас до нього."""
    v = (value or "/").strip()
    if not v.startswith("/") or v.startswith("//") or "://" in v or "\\" in v:
        return "/"
    return v


LOGIN_PAGE = """<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Вход · Matro Dashboard</title>
<link rel="icon" href="/favicon.ico">
<style>
  :root{
    --bg:#070B14; --card:#101522; --border:rgba(255,255,255,.06);
    --border-strong:rgba(255,255,255,.14);
    --ink:#F7F8FB; --ink-soft:#95A0BF; --ink-dim:#5A6A8A;
    --accent:#FF8C42; --accent-2:#FF6A2B; --danger:#FF5E6C;
  }
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:flex;align-items:center;
    justify-content:center;padding:24px;background:var(--bg);color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
    /* Два м'які відблиски у фірмових кольорах — щоб сторінка не була
       просто чорним прямокутником з полем для пароля. */
    background-image:radial-gradient(900px 500px at 15% -10%,rgba(255,140,66,.10),transparent 60%),
      radial-gradient(800px 500px at 100% 110%,rgba(91,141,238,.10),transparent 60%)}
  .box{width:100%;max-width:360px}
  /* Той самий знак, що в шапці дашборду (home.html): «Matro» з помаранчевою
     крапкою — щоб сторінка входу не виглядала чужою. */
  .brand{margin:0 0 24px;text-align:center}
  .brand b{font-size:21px;font-weight:700;letter-spacing:-.3px}
  .brand b i{font-style:normal;color:var(--accent)}
  .brand span{display:block;margin-top:5px;font-size:11px;font-weight:600;
    color:var(--ink-dim);text-transform:uppercase;letter-spacing:1.3px}
  form{background:var(--card);border:1px solid var(--border);border-radius:16px;
    padding:24px 22px;box-shadow:0 24px 60px rgba(0,0,0,.45)}
  h1{margin:0 0 4px;font-size:18px;font-weight:650;letter-spacing:-.2px}
  .hint{margin:0 0 20px;font-size:12.5px;color:var(--ink-dim);line-height:1.5}
  label{display:block;font-size:11px;font-weight:650;text-transform:uppercase;
    letter-spacing:.9px;color:var(--ink-dim);margin:0 0 7px}
  input{width:100%;font:inherit;font-size:14.5px;color:var(--ink);
    background:rgba(255,255,255,.03);border:1px solid var(--border-strong);
    border-radius:10px;padding:11px 13px;margin:0 0 16px;transition:border-color 150ms}
  input:focus{outline:none;border-color:var(--accent)}
  button{width:100%;font:inherit;font-size:14.5px;font-weight:650;color:#fff;
    background:linear-gradient(135deg,var(--accent),var(--accent-2));border:0;
    border-radius:10px;padding:12px;cursor:pointer;transition:filter 150ms}
  button:hover{filter:brightness(1.07)}
  .err{display:flex;gap:9px;align-items:flex-start;font-size:13px;
    color:var(--danger);background:rgba(255,94,108,.09);
    border:1px solid rgba(255,94,108,.25);border-radius:10px;
    padding:10px 12px;margin:0 0 18px;line-height:1.45}
  .foot{margin:16px 0 0;font-size:11.5px;color:var(--ink-dim);text-align:center}
</style></head>
<body>
  <div class="box">
    <div class="brand">
      <b>Matro<i>.</i></b><span>Digital Operations Center</span>
    </div>
    <form method="post" action="/login">
      <h1>Вход</h1>
      <p class="hint">Доступ только для сотрудников. Логин и пароль выдаёт руководитель.</p>
      __ERROR__
      <input type="hidden" name="next" value="__NEXT__">
      <label for="u">Логин</label>
      <input id="u" name="username" value="__USER__" autocomplete="username"
             autocapitalize="off" autocorrect="off" spellcheck="false" required autofocus>
      <label for="p">Пароль</label>
      <input id="p" name="password" type="password" autocomplete="current-password" required>
      <button type="submit">Войти</button>
    </form>
    <p class="foot">Вход запоминается на 30 дней</p>
  </div>
</body></html>
"""


def _login_page(error="", nxt="/", code=200, user=""):
    # escape: усе, що підставляємо, потрапляє в HTML — у тому числі логін,
    # який ввела людина, тобто будь-який рядок із форми.
    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))
    html = (LOGIN_PAGE
            .replace("__ERROR__", f'<div class="err">{esc(error)}</div>' if error else "")
            .replace("__NEXT__", esc(nxt))
            # Після помилки логін лишається в полі: друкувати його заново
            # через одну одруківку в паролі — зайве роздратування.
            .replace("__USER__", esc(user[:80])))
    resp = Response(html, code, {"Content-Type": "text/html; charset=utf-8"})
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/login", methods=["GET", "POST"])
def login():
    if not DASH_PASS:
        return Response("DASHBOARD_PASSWORD не задано на сервері.", 500)

    nxt = _safe_next(request.values.get("next", "/"))
    scope = current_scope()
    if scope == "all":
        return redirect(nxt, code=302)

    if request.method == "GET":
        return _login_page(nxt=nxt)

    ip = _client_ip()
    if _throttled(ip):
        return _login_page("Слишком много попыток. Попробуйте через 15 минут.", nxt, 429,
                           request.form.get("username", ""))

    got = check_login(request.form.get("username", ""), request.form.get("password", ""))
    if not got:
        _note_fail(ip)
        print(f"[login] невдалий вхід: {ip}", flush=True)
        return _login_page("Неверный логин или пароль.", nxt, 401,
                           request.form.get("username", ""))

    # Актор зі своїм паролем не має куди йти, крім сторінки озвучки.
    target = nxt if got == "all" else (f"/voice/{VOICE_KEY}/" if VOICE_KEY else "/login")
    if got == "voice" and not nxt.startswith("/voice/"):
        nxt = target
    print(f"[login] вхід ({got}): {ip}", flush=True)

    resp = redirect(nxt if got == "all" else target, code=302)
    resp.set_cookie(
        COOKIE_NAME, _signer.dumps({"s": got, "t": int(time.time())}),
        max_age=SESSION_DAYS * 86400, httponly=True, samesite="Lax",
        # Прапорець secure тільки на https: на Render TLS знімає проксі, тому
        # дивимось на X-Forwarded-Proto, а не на схему самого запиту. Локально
        # по http cookie так само працює.
        secure=(request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https"
                or request.is_secure),
    )
    return resp


@app.route("/logout")
def logout():
    resp = redirect("/login", code=302)
    resp.delete_cookie(COOKIE_NAME, samesite="Lax")
    return resp


# Іконка — без пароля: інакше на сторінці входу браузер отримує на неї 401
# і малює порожній квадрат у вкладці.
@app.route("/favicon.ico")
def favicon_route():
    return send_from_directory(BASE_DIR, "favicon.ico")


# ── Раздача сторінок дашборду (з паролем) ────────────────────────────────
@app.route("/")
@requires_auth
def index_route():
    return send_from_directory(BASE_DIR, "home.html")

# ── Сторінка з текстами для озвучки ──────────────────────────────────────
#
# Два замки, і потрібні обидва: секретне посилання з ключем
#
#     https://…/voice/<VOICE_KEY>/
#
# плюс логін. Раніше тут був тільки ключ, без пароля — посилання, яке
# переслали в месенджері, відкривалось у будь-кого, хто його побачив.
#
# Актор — людина не з команди, давати їй пароль від усього дашборду (де CRM,
# суми, менеджери) не можна. Тому для нього є окрема пара VOICE_USER /
# VOICE_PASSWORD, яка відкриває рівно цю сторінку й більше нічого. Якщо
# VOICE_PASSWORD не заданий — сторінка вимагає звичайний логін дашборду.
#
# Ключ лежить у змінній середовища на Render, а не в коді: репозиторій
# публічний. Якщо VOICE_KEY не задано — маршрут просто не працює (404), щоб
# сторінка не виявилась відкритою «за замовчуванням».
#
# Віддаємо лише білий список файлів: саму сторінку, її дані й аудіо. Інакше
# цей маршрут став би діркою в авторизації — через нього можна було б
# викачати bitrix_data.json так само легко, як mp3.
VOICE_KEY = os.environ.get("VOICE_KEY", "").strip()
VOICE_FILES = {"voice.html", "voice_data.json", "favicon.ico"}


@app.route("/voice/<key>")
def voice_redirect(key):
    # Без кінцевого слеша відносні шляхи на сторінці ('voice_data.json',
    # 'audio/…') розв'язувались би від /voice/, а не від /voice/<key>/.
    return redirect(f"/voice/{key}/", code=302)


@app.route("/voice/<key>/", defaults={"filename": "voice.html"})
@app.route("/voice/<key>/<path:filename>")
def voice_public(key, filename):
    if not VOICE_KEY or not hmac.compare_digest(str(key), VOICE_KEY):
        return "not found", 404
    if current_scope() not in ("all", "voice"):
        return _deny()
    if filename not in VOICE_FILES and not filename.startswith("audio/"):
        return "not found", 404
    safe = (BASE_DIR / filename).resolve()
    if BASE_DIR.resolve() not in safe.parents:
        return "not found", 404
    resp = send_from_directory(BASE_DIR, filename)
    # Щоб посилання не спливло в пошуку, якщо актор кудись його вставить.
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@app.route("/<path:filename>")
@requires_auth
def static_route(filename):
    # На всякий випадок не віддаємо файли поза папкою проєкту й нічого з
    # прихованих/системних шляхів.
    safe = (BASE_DIR / filename).resolve()
    if BASE_DIR.resolve() not in safe.parents and safe != BASE_DIR.resolve():
        return "not found", 404
    return send_from_directory(BASE_DIR, filename)

# ── FAQ: правила роботи на сторінці менеджерів ───────────────────────────
# Дані лежать у Google Таблиці (див. faq_store.py) — диск на Render
# одноразовий, і правило, додане менеджером, інакше зникло б при найближчому
# передеплої.
import faq_store


@app.route("/api/faq", methods=["GET"])
@requires_auth
def faq_list():
    return jsonify({"items": faq_store.list_items(), **faq_store.status()})


@app.route("/api/faq", methods=["POST"])
@requires_auth
def faq_add():
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    answer = (data.get("answer") or "").strip()
    if not question or not answer:
        return jsonify({"error": "Питання і відповідь не можуть бути порожніми"}), 400
    if len(question) > 300 or len(answer) > 4000:
        return jsonify({"error": "Занадто довгий текст"}), 400
    try:
        item = faq_store.add_item(data.get("author", ""), question, answer)
    except faq_store.StorageError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"item": item, **faq_store.status()}), 201


@app.route("/api/faq/<item_id>", methods=["PUT"])
@requires_auth
def faq_update(item_id):
    data = request.get_json(silent=True) or {}
    try:
        item = faq_store.update_item(
            item_id,
            question=data.get("question"),
            answer=data.get("answer"),
            author=data.get("author"),
        )
    except faq_store.StorageError as e:
        return jsonify({"error": str(e)}), 502
    if not item:
        return jsonify({"error": "Запис не знайдено"}), 404
    return jsonify({"item": item})


@app.route("/api/faq/<item_id>", methods=["DELETE"])
@requires_auth
def faq_delete(item_id):
    try:
        removed = faq_store.delete_item(item_id)
    except faq_store.StorageError as e:
        return jsonify({"error": str(e)}), 502
    if not removed:
        return jsonify({"error": "Запис не знайдено"}), 404
    return jsonify({"status": "deleted"})


# ── Налаштування балів менеджерів ───────────────────────────────────────
# Ваги спільні для всіх, тому лежать у таблиці, а не в браузері (див.
# points_store.py).
import points_store


@app.route("/api/points/settings", methods=["GET"])
@requires_auth
def points_settings_get():
    return jsonify({"settings": points_store.get_settings(), **points_store.status()})


@app.route("/api/points/settings", methods=["POST"])
@requires_auth
def points_settings_set():
    data = request.get_json(silent=True) or {}
    try:
        saved = points_store.save_settings(data)
    except points_store.StorageError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"settings": saved, **points_store.status()})


@app.route("/api/data/status")
@requires_auth
def data_status():
    """Що є на диску і чим це зібрано. Відповідає на питання «чому немає
    даних» без походу в логи Render."""
    files = {}
    for name, script in FILE_OWNER.items():
        f = BASE_DIR / name
        info = {"exists": f.exists(), "script": script}
        if f.exists():
            st = f.stat()
            info["size_mb"] = round(st.st_size / 1048576, 2)
            info["age_minutes"] = round((time.time() - st.st_mtime) / 60)
        info["last_run"] = LAST_RUN.get(script)
        files[name] = info
    return jsonify({"files": files, "server_time": time.strftime("%Y-%m-%d %H:%M:%S")})


@app.route("/healthz")
def healthz():
    # Без пароля — Render використовує це, щоб перевіряти, що сервіс живий.
    return "ok", 200

# ── Ringostat: "хто на лінії зараз" — живий запит, НЕ через розклад ─────────
# Раз-на-3-години кеш для цього показника марний (менеджер, який щойно
# завершив дзвінок, буде показаний як "зайнятий" ще годинами) — тому
# ringostat.html питає це напряму в момент відкриття/оновлення сторінки,
# а сервер лише проксує запит до Ringostat, щоб ключ (RINGOSTAT_AUTH_KEY)
# не потрапив у клієнтський JS.
RINGOSTAT_AUTH_KEY = os.environ.get("RINGOSTAT_AUTH_KEY", "")

@app.route("/api/ringostat/status")
@requires_auth
def ringostat_status():
    if not RINGOSTAT_AUTH_KEY:
        return jsonify({"error": "RINGOSTAT_AUTH_KEY не задано на сервері"}), 500
    headers = {
        "Auth-key": RINGOSTAT_AUTH_KEY,
        "User-Agent": "Mozilla/5.0 (compatible; MatroDashboardBot/1.0)",
    }
    try:
        online = requests.get("https://api.ringostat.net/sipstatus/online", headers=headers, timeout=10)
        online.raise_for_status()
        speaking = requests.get("https://api.ringostat.net/sipstatus/speaking", headers=headers, timeout=10)
        speaking.raise_for_status()
        return jsonify({"online": online.json(), "speaking": speaking.json()})
    except Exception as e:
        return jsonify({"error": str(e)}), 502

# ── Діагностика полів Ringostat ────────────────────────────────────────
# Навіщо це потрібно. Колонка «Дзвінки» в таблиці магазину будується з
# того, З ЯКОГО САЙТУ прийшов дзвінок. У вигрузці звіту ця ознака є —
# стовпці pool_name і page, — але в API вони звуться інакше: запит із
# ними повертає не JSON, а порожню відповідь, і збір мовчки відкочується
# на бідніший набір полів.
#
# Тому маршрут перебирає кандидатів по одному: на кожне ім'я йде окремий
# запит «calldate + кандидат». Якщо API віддав JSON — ім'я існує, і видно,
# чи є в ньому значення. Якщо ні — такого поля немає. Так за один захід
# стає ясно, як насправді називається сайт дзвінка.
#
# Що НЕ повертаємо: номери телефонів і повні адреси сторінок (у них
# прилітають рекламні мітки). Тільки імена полів, короткі значення
# та домени.
# Підтверджені перебором: pool_name, referrer, n_alias, utm_campaign,
# utm_content, utm_term. Решта — чергові кандидати на «сторінку дзвінка»,
# якої в API поки не знайшлося. Свій список можна передати параметром
# ?f=ім'я,ім'я — щоб шукати далі без нового деплою.
RINGOSTAT_FIELD_CANDIDATES = [
    "pool_name", "referrer", "n_alias",
    "call_page", "page_title", "landing", "landing_url", "location",
    "uri", "request_uri", "href", "web_page", "site_url", "source_page",
    "visit_page", "first_visit_page", "entrance", "entry", "recording",
    "connected_with", "duration", "tags", "comment",
]


@app.route("/api/ringostat/probe")
@requires_auth
def ringostat_probe():
    if not RINGOSTAT_AUTH_KEY:
        return jsonify({"error": "RINGOSTAT_AUTH_KEY не задано на сервері"}), 500

    project_id = os.environ.get("RINGOSTAT_PROJECT_ID", "")
    headers = {
        "Auth-key": RINGOSTAT_AUTH_KEY,
        "User-Agent": "Mozilla/5.0 (compatible; MatroDashboardBot/1.0)",
    }
    now = time.time()
    frm = time.strftime("%Y-%m-%d 00:00:00", time.localtime(now - 86400))
    to = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))

    # ?f=ім'я,ім'я — перевірити свій список замість типового.
    asked = [n.strip() for n in (request.args.get("f") or "").split(",") if n.strip()]
    candidates = asked[:40] or RINGOSTAT_FIELD_CANDIDATES

    def ask(fields):
        """Повертає (рядки, текст помилки). Помилка — це і не-JSON теж."""
        params = {"token": RINGOSTAT_AUTH_KEY, "project_id": project_id,
                  "export_type": "json", "from": frm, "to": to}
        if fields:
            params["fields"] = fields
        try:
            r = requests.get("https://api.ringostat.net/calls/list",
                             params=params, headers=headers, timeout=45)
            body = (r.text or "").strip()
            if not body:
                return [], f"порожня відповідь (HTTP {r.status_code})"
            try:
                data = r.json()
            except Exception:
                return [], f"не JSON (HTTP {r.status_code}): {body[:120]}"
            if isinstance(data, dict):
                if "error" in data:
                    return [], str(data["error"])[:200]
                rows = next((data[k] for k in ("result", "calls", "data", "items")
                             if isinstance(data.get(k), list)), [])
            else:
                rows = data if isinstance(data, list) else []
            return [r for r in rows if isinstance(r, dict)], None
        except Exception as e:
            return [], str(e)[:200]

    def short(values):
        """Короткі значення — як є; схоже на адресу — лише домен; схоже на
        номер телефону — нічого. Діагностика не привід світити номери."""
        out = set()
        for v in values:
            v = str(v)
            digits = sum(c.isdigit() for c in v)
            if digits >= 7 and digits >= len(v) - 3:
                out.add("…")
            elif "/" in v or v.count(".") >= 2:
                try:
                    out.add(urllib.parse.urlparse(
                        v if "//" in v else "//" + v).hostname or v.split("/")[0])
                except Exception:
                    out.add(v.split("/")[0])
            else:
                out.add(v[:40])
        return sorted(out)[:12]

    base_rows, base_err = ask(None)
    base = {"рядків": len(base_rows), "помилка": base_err,
            "ключі": sorted({k for r in base_rows[:200] for k in r})}
    # Поля з номером клієнта показуємо лише лічильником.
    PHONE_KEYS = {"caller", "dst", "phone", "client_phone", "contact_phone", "num"}
    for key in base["ключі"]:
        vals = [r.get(key) for r in base_rows if r.get(key)]
        base[key] = {"заповнено": len(vals)}
        if key not in PHONE_KEYS:
            base[key]["значення"] = short(vals)

    found, missing = {}, []
    for name in candidates:
        rows, err = ask("calldate," + name)
        if err:
            missing.append({name: err})
            continue
        vals = [r.get(name) for r in rows if r.get(name)]
        found[name] = {"рядків": len(rows), "заповнено": len(vals),
                       "значення": short(vals)}
        if name in PHONE_KEYS:
            found[name].pop("значення")

    return jsonify({"період": [frm, to],
                    "без_параметра_fields": base,
                    "поля_які_існують": found,
                    "поля_яких_немає": missing})


# ── Аудит сайту: сканування карток товарів matroluxe.ua ─────────────────
# Скан обходить ~360 сторінок і триває кілька хвилин, тому по кнопці ми
# лише СТАРТУЄМО його у фоні й одразу відповідаємо. Сторінка потім сама
# питає /api/audit/status, щоб малювати прогрес — інакше HTTP-запит висів
# би хвилинами й його прибив би таймаут проксі Render.
_audit_lock = threading.Lock()
_audit_running = False

def _audit_worker():
    global _audit_running
    try:
        run_script("audit.py", timeout=3600)
    finally:
        with _audit_lock:
            _audit_running = False

def start_audit():
    """Повертає True, якщо скан запущено, і False — якщо він уже йде."""
    global _audit_running
    with _audit_lock:
        if _audit_running:
            return False
        _audit_running = True
    threading.Thread(target=_audit_worker, daemon=True).start()
    return True

def scheduled_audit():
    if not start_audit():
        print("[scheduler] аудит вже виконується — пропускаю запуск за розкладом", flush=True)

# Фід описів. Завантаження й розбір — секунди, тому окрема кнопка: після
# того, як контент-менеджер залив описи, він має побачити, що лічильник
# зменшився, не чекаючи ні нічного запуску, ні дев'ятихвилинного скану сайту.
_feed_running = False
_feed_lock = threading.Lock()


def _feed_worker():
    global _feed_running
    try:
        run_script("fetch_feed.py", timeout=300)
    finally:
        with _feed_lock:
            _feed_running = False


@app.route("/api/feed/refresh", methods=["POST"])
@requires_auth
def feed_refresh():
    global _feed_running
    with _feed_lock:
        if _feed_running:
            return jsonify({"status": "already_running"}), 409
        _feed_running = True
    threading.Thread(target=_feed_worker, daemon=True).start()
    return jsonify({"status": "started"}), 202


@app.route("/api/feed/status")
@requires_auth
def feed_status():
    stamp = ""
    data = _read_json("feed_data.json")
    if data:
        stamp = data.get("generated_at", "")
    with _feed_lock:
        running = _feed_running
    return jsonify({"running": running, "generated_at": stamp,
                    "last_run": LAST_RUN.get("fetch_feed.py")})


@app.route("/api/audit/scan", methods=["POST"])
@requires_auth
def audit_scan():
    if not start_audit():
        return jsonify({"status": "already_running"}), 409
    return jsonify({"status": "started"}), 202

# ── Позначки роботи над описами ─────────────────────────────────────────
# Сканер бачить лише результат на сайті, а позначки закривають проміжок між
# «текст написали» і «текст залили» (див. audit_store.py).
import audit_store


def _current_problem_urls():
    """Що саме зараз вважається проблемним — і за сторінками сайту, і за
    товарним фідом. Повертає (множина ключів, які списки прочитались).

    Список, який не прочитався, позначаємо як невідомий, і audit_store не
    закриває позначки з нього. Інакше одна збійна ніч оголосила б усі
    незавершені картки зробленими — їх же немає в списку проблем.
    """
    urls, scopes = set(), {"page": False, "feed": False}

    data = _read_json("audit_data.json")
    if data is not None:
        urls |= {i.get("url") for i in (data.get("items") or []) if i.get("url")}
        scopes["page"] = True

    feed = _read_json("feed_data.json")
    if feed is not None:
        urls |= {i.get("key") for i in (feed.get("items") or []) if i.get("key")}
        scopes["feed"] = True

    if not any(scopes.values()):
        return None, None
    return urls, scopes


def _read_json(name):
    f = BASE_DIR / name
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


@app.route("/api/audit/marks", methods=["GET"])
@requires_auth
def audit_marks_list():
    urls, scopes = _current_problem_urls()
    return jsonify({"marks": audit_store.list_marks(urls, scopes),
                    **audit_store.status()})


@app.route("/api/audit/marks", methods=["POST"])
@requires_auth
def audit_marks_set():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    new_status = (data.get("status") or "").strip()
    if not url:
        return jsonify({"error": "Не вказано адресу товару"}), 400
    if new_status and new_status not in audit_store.VALID_STATUSES:
        return jsonify({"error": f"Невідомий статус: {new_status}"}), 400
    try:
        if not new_status:
            audit_store.clear_mark(url)
            return jsonify({"status": "cleared"})
        mark = audit_store.set_mark(url, new_status,
                                    data.get("author", ""), data.get("note", ""))
    except audit_store.StorageError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"mark": mark})


@app.route("/api/audit/status")
@requires_auth
def audit_status():
    # Прогрес пише сам audit.py; чи процес ще живий — знає тільки сервер,
    # тому "running" беремо звідси, а не з файлу (інакше впалий скан
    # назавжди залишався б "у процесі").
    progress = {}
    pf = BASE_DIR / "audit_progress.json"
    if pf.exists():
        try:
            progress = json.loads(pf.read_text(encoding="utf-8"))
        except Exception:
            progress = {}
    with _audit_lock:
        running = _audit_running
    return jsonify({
        "running": running,
        "phase": progress.get("phase"),
        "done": progress.get("done"),
        "total": progress.get("total"),
    })

# ── Планувальник: fetch_meta/gsc/bitrix раз на 3 години ─────────────────
scheduler = BackgroundScheduler()
scheduler.add_job(run_fast_periodic, "interval", minutes=30, id="fast_fetch",
                  max_instances=1, coalesce=True)
scheduler.add_job(run_slow_periodic, "interval", hours=3, id="slow_fetch",
                  max_instances=1, coalesce=True)
# Аудит — раз на добу о 01:00 UTC (04:00 за Києвом): вночі і сайт вільний,
# і зранку на дашборді вже свіжі дані.
scheduler.add_job(scheduled_audit, "cron", hour=1, minute=0, id="daily_audit")
scheduler.start()


# ── Перший прогін після старту ──────────────────────────────────────────
# Раніше важкий збір стартував прямо в момент запуску сервера. На інстансі
# з 512 МБ це найгірший можливий момент: процес ще піднімається, Render
# чекає відповіді від /healthz, а поруч уже працюють fetch-скрипти. Якщо
# памʼяті не вистачить саме там — контейнер гине ДО того, як сервіс устиг
# піднятись, Render пробує знову, і сервіс назавжди лишається "Failed".
#
# Тому тепер: спершу дати серверу піднятись і пройти перевірку здоровʼя, і
# лише потім братись за дані. І не братись взагалі, якщо дані свіжі — після
# деплою файли приїжджають з репозиторію, куди їх щойно поклав GitHub
# Actions, і перезбирати їх одразу нема сенсу.
FIRST_RUN_DELAY_SEC = 180
FRESH_HOURS = 6


# ringostat_data.json у репозиторій не комітиться — він зʼявляється лише
# після запуску fetch_ringostat.py на сервері. Після деплою диск скидається
# до версії з GitHub, тобто журналу дзвінків на ньому немає взагалі.
NEEDED_FILES = ("bitrix_data.json", "seo_data.json", "data.json", "ringostat_data.json")


def _file_age_hours(path):
    """Скільки годин даним у файлі — за полем generated_at усередині, а не
    за часом файлу.

    Чому не mtime. Диск на Render одноразовий: при кожному деплої файли
    заново викачуються з репозиторію, і час їх створення дорівнює часу
    деплою. Тобто одразу після пушу файл із даними за 11:43 виглядає
    «щойно оновленим», перевірка свіжості каже «все гаразд», стартовий
    збір пропускається — і на дашборді висять вчорашні цифри, хоча сервер
    тільки-но перезапустився. Саме це й виглядало як «я ж щойно оновлював,
    чому знову треба».

    Читаємо лише початок файлу: generated_at стоїть першим полем, а
    seo_data.json важить 16 МБ і розбирати його цілком заради однієї дати
    безглуздо.
    """
    try:
        with open(path, "rb") as fp:
            head = fp.read(4096).decode("utf-8", "ignore")
        m = re.search(r'"generated_at"\s*:\s*"([^"]+)"', head)
        if m:
            raw = m.group(1).strip()
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    stamp = datetime.strptime(raw, fmt)
                    return (datetime.now() - stamp).total_seconds() / 3600
                except ValueError:
                    continue
    except Exception:
        pass
    # Немає поля або не розібрали — лишається час файлу, як було раніше.
    return (time.time() - path.stat().st_mtime) / 3600


def _data_is_fresh():
    """True, лише якщо ВСІ потрібні файли на місці й дані в них свіжі.

    Відсутній файл — це не «свіжо», це «нема чого показувати». Інакше
    виходить пастка: три файли приїхали свіжими з репозиторію, перевірка
    каже «все гаразд», стартовий збір пропускається, а журнал дзвінків,
    якого в репозиторії немає й не було, не зʼявляється ще три години.
    """
    oldest = 0.0
    for name in NEEDED_FILES:
        f = BASE_DIR / name
        if not f.exists():
            print(f"[scheduler] немає {name} — збір потрібен", flush=True)
            return False
        age = _file_age_hours(f)
        if age > FRESH_HOURS:
            print(f"[scheduler] {name}: даним {age:.1f} год — збір потрібен", flush=True)
            return False
        oldest = max(oldest, age)
    print(f"[scheduler] найстаріші дані: {oldest:.1f} год", flush=True)
    return True


# Який скрипт відповідає за який файл. Потрібно, щоб після деплою спершу
# зібрати те, чого на диску НЕМАЄ, а не йти по черзі з початку.
FILE_OWNER = {
    "feed_data.json": "fetch_feed.py",
    "ringostat_data.json": "fetch_ringostat.py",
    "bitrix_data.json": "fetch_bitrix.py",
    "seo_data.json": "fetch_gsc.py",
    "data.json": "fetch_meta.py",
}


def _first_run():
    time.sleep(FIRST_RUN_DELAY_SEC)

    # Файли, яких на диску немає. Після деплою це завжди ringostat_data.json:
    # його не комітять у репозиторій, тож він зникає при кожному передеплої.
    # Раніше він стояв останнім у черзі — після meta, SEO і Bitrix, який ще й
    # тягне контакти, — і сторінка балів хвилин п'ятнадцять показувала
    # «журнал дзвінків недоступний». Тому спершу добираємо відсутнє.
    missing = [n for n in NEEDED_FILES if not (BASE_DIR / n).exists()]
    for name in missing:
        script = FILE_OWNER.get(name)
        if script:
            print(f"[scheduler] немає {name} — збираю {script} першим", flush=True)
            run_script(script)

    if _data_is_fresh():
        print(f"[scheduler] решта даних свіжіша за {FRESH_HOURS} год — повний збір пропускаю", flush=True)
        return
    print("[scheduler] стартовий збір даних", flush=True)
    run_fast_periodic()
    run_slow_periodic()


threading.Thread(target=_first_run, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
