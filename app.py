"""
app.py — веб-сервер для розгортання matro-dashboard на Render.com замість
GitHub Pages. Робить три речі:

1. Віддає ті самі HTML/JSON файли дашборду, що й раніше — але тепер за
   паролем (Basic Auth), бо сервер більше не публічний статичний сайт.
2. Приймає вихідний вебхук з Bitrix24 (/webhook/bitrix) — при кожній зміні
   угоди/ліда Bitrix сам стукається сюди, і ми одразу перезапускаємо
   fetch_bitrix.py, замість того щоб чекати наступного розкладу.
3. Сам кличе fetch_meta.py / fetch_gsc.py / fetch_bitrix.py за розкладом
   (раз на 3 години) — так само, як раніше це робив GitHub Actions,
   просто тепер планувальник крутиться прямо тут, у самому сервері.
"""
import os, sys, time, json, threading, subprocess
from pathlib import Path
from functools import wraps

import requests
from flask import Flask, request, send_from_directory, Response, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

BASE_DIR = Path(__file__).parent
app = Flask(__name__, static_folder=None)

# ── Пароль на весь дашборд ───────────────────────────────────────────────
DASH_USER = os.environ.get("DASHBOARD_USER", "admin")
DASH_PASS = os.environ.get("DASHBOARD_PASSWORD", "")

def check_auth(username, password):
    return bool(DASH_PASS) and username == DASH_USER and password == DASH_PASS

def authenticate():
    return Response(
        "Потрібна авторизація для перегляду дашборду.",
        401, {"WWW-Authenticate": 'Basic realm="Matro Dashboard"'},
    )

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not DASH_PASS:
            # Пароль не заданий у змінних середовища — навмисно НЕ пускаємо
            # нікого, щоб не залишити дашборд відкритим "по замовчуванню".
            return Response("DASHBOARD_PASSWORD не задано на сервері.", 500)
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

# ── Фонові прогони fetch-скриптів ────────────────────────────────────────
def run_script(name, timeout=1200):
    print(f"[scheduler] запускаю {name}…", flush=True)
    try:
        result = subprocess.run([sys.executable, str(BASE_DIR / name)], check=False, timeout=timeout)
        # Раніше тут писали "завершено" незалежно від коду завершення —
        # якщо скрипт падав з необробленим винятком (traceback), лог все
        # одно виглядав так, ніби все пройшло успішно, і це маскувало
        # реальні збої (саме так довго непомітно ламався fetch_meta.py).
        if result.returncode == 0:
            print(f"[scheduler] {name} завершено", flush=True)
        else:
            print(f"[scheduler] ✗ {name} ЗАВЕРШИВСЯ З ПОМИЛКОЮ (код {result.returncode}) — дивись traceback вище", flush=True)
    except Exception as e:
        print(f"[scheduler] {name} впав: {e}", flush=True)

def run_all_periodic():
    run_script("fetch_meta.py")
    run_script("fetch_gsc.py")
    run_script("fetch_bitrix.py")
    run_script("fetch_ringostat.py")

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

# ── Раздача сторінок дашборду (з паролем) ────────────────────────────────
@app.route("/")
@requires_auth
def index_route():
    return send_from_directory(BASE_DIR, "home.html")

@app.route("/<path:filename>")
@requires_auth
def static_route(filename):
    # На всякий випадок не віддаємо файли поза папкою проєкту й нічого з
    # прихованих/системних шляхів.
    safe = (BASE_DIR / filename).resolve()
    if BASE_DIR.resolve() not in safe.parents and safe != BASE_DIR.resolve():
        return "not found", 404
    return send_from_directory(BASE_DIR, filename)

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

@app.route("/api/audit/scan", methods=["POST"])
@requires_auth
def audit_scan():
    if not start_audit():
        return jsonify({"status": "already_running"}), 409
    return jsonify({"status": "started"}), 202

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
scheduler.add_job(run_all_periodic, "interval", hours=3, id="periodic_fetch")
# Аудит — раз на добу о 01:00 UTC (04:00 за Києвом): вночі і сайт вільний,
# і зранку на дашборді вже свіжі дані.
scheduler.add_job(scheduled_audit, "cron", hour=1, minute=0, id="daily_audit")
scheduler.start()
# Перший прогін одразу при старті сервера, щоб дані з'явились без очікування 3 годин.
threading.Thread(target=run_all_periodic, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
