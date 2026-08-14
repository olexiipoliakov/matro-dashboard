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
import os, sys, time, threading, subprocess
from pathlib import Path
from functools import wraps

from flask import Flask, request, send_from_directory, Response
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
        subprocess.run([sys.executable, str(BASE_DIR / name)], check=False, timeout=timeout)
        print(f"[scheduler] {name} завершено", flush=True)
    except Exception as e:
        print(f"[scheduler] {name} впав: {e}", flush=True)

def run_all_periodic():
    run_script("fetch_meta.py")
    run_script("fetch_gsc.py")
    run_script("fetch_bitrix.py")

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

# ── Планувальник: fetch_meta/gsc/bitrix раз на 3 години ─────────────────
scheduler = BackgroundScheduler()
scheduler.add_job(run_all_periodic, "interval", hours=3, id="periodic_fetch")
scheduler.start()
# Перший прогін одразу при старті сервера, щоб дані з'явились без очікування 3 годин.
threading.Thread(target=run_all_periodic, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
