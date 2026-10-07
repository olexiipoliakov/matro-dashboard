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
scheduler.add_job(run_all_periodic, "interval", hours=3, id="periodic_fetch")
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


def _data_is_fresh():
    """True, лише якщо ВСІ потрібні файли на місці й оновлювались нещодавно.

    Відсутній файл — це не «свіжо», це «нема чого показувати». Інакше
    виходить пастка: три файли приїхали свіжими з репозиторію, перевірка
    каже «все гаразд», стартовий збір пропускається, а журнал дзвінків,
    якого в репозиторії немає й не було, не зʼявляється ще три години.
    """
    newest = 0.0
    for name in NEEDED_FILES:
        f = BASE_DIR / name
        if not f.exists():
            print(f"[scheduler] немає {name} — збір потрібен", flush=True)
            return False
        newest = max(newest, f.stat().st_mtime)
    return (time.time() - newest) < FRESH_HOURS * 3600


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
    run_all_periodic()


threading.Thread(target=_first_run, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
