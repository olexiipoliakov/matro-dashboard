"""
fetch_ringostat.py — тягне статистику дзвінків (вхідні/вихідні/пропущені) з
Ringostat API в розрізі менеджерів і зберігає ringostat_data.json.

Формат виводу — той самий "сирий" підхід, що й у fetch_bitrix.py: один рядок
на дзвінок, а не готові агрегати. Це дозволяє ringostat.html самостійно
перерахувати будь-яку розбивку під довільно вибраний період на клієнті.

Важливо: "хто на лінії зараз" (реальний час) сюди НЕ входить — це окремий
живий запит з app.py (/api/ringostat/status) прямо в момент відкриття
сторінки, бо раз-на-3-години розклад для "онлайн зараз" безглуздий.

Налаштування (Render → Environment, секрети, в код не вписувати):
  RINGOSTAT_AUTH_KEY   — ключ доступу до API (Ringostat → Налаштування →
                          Інтеграції/API).
  RINGOSTAT_PROJECT_ID — ID проєкту (там же).

2026-08-19 — ВИПРАВЛЕННЯ ЗІСТАВЛЕННЯ ДЗВІНОК→МЕНЕДЖЕР:
Початкова версія зіставляла дзвінок з менеджером за номерами полів
caller/dst проти довідника напрямків співробітника (SIP-логіни/номери).
Реальні логи Render показали, що це не працює (наприклад дзвінки з/на
9460, 9465 лишались "без прив'язки" — unmatched). Причина: поля caller/dst
у відповіді Ringostat — це номер КЛІЄНТА і номер ЛІНІЇ ВІДСТЕЖЕННЯ
(tracking-номер), а зовсім не внутрішній номер співробітника.

Офіційний довідник параметрів calls/list (Google Sheet Ringostat) прямо
називає правильні поля для прив'язки дзвінка до співробітника:
  employee_number — ID співробітника (те саме staffId, що й у
                     getProjectStaffListAndDirections)
  employee_fio     — ПІБ співробітника (як він записаний у Ringostat)
  call_type        — тип дзвінка: in / out / callback / transitin / transitout

Тому зіставлення тепер іде напряму через employee_number (без жодних
евристик по номерах), а напрям (вхідний/вихідний) береться з call_type.
Довідник directions_exact лишається в meta лише для живого "хто на лінії
зараз" (звірка SIP-логінів з /api/ringostat/status), для самих дзвінків
він більше не використовується.
"""
import csv, io, json, os, re, sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

# Один спосіб рахувати ключ телефону для обох збирачів — див. phone_key.py
from phone_key import phone_key
import requests

AUTH_KEY = os.environ.get("RINGOSTAT_AUTH_KEY", "")
PROJECT_ID = os.environ.get("RINGOSTAT_PROJECT_ID", "")

# Посилання на вигрузку Ringostat у Google Sheets (export?format=csv).
# ТІЛЬКИ через оточення: у таблиці лежать номери клієнтів у відкритому
# вигляді, а репозиторій публічний — посилання в коді означало б, що їх
# може завантажити будь-хто.
SHEET_CSV_URL = os.environ.get("RINGOSTAT_SHEET_CSV", "")

API_BASE = "https://api.ringostat.net"
HTTP_HEADERS = {
    "Auth-key": AUTH_KEY,
    "User-Agent": "Mozilla/5.0 (compatible; MatroDashboardBot/1.0)",
}

DAYS_BACK = 90  # той самий горизонт, що й у fetch_bitrix.py
CHUNK_DAYS = 7  # тягнемо calls/list тижневими вікнами, щоб не впертися в ліміти відповіді

OUT = Path(__file__).parent / "ringostat_data.json"

# Офіційно задокументовані статуси дзвінка (Ringostat Knowledge Base,
# "Call log. Description of call statuses"). Нормалізуємо пробіли/`+`/`_`,
# бо в різних місцях документації значення показані по-різному закодованими.
ANSWERED_STATUSES = {"ANSWERED", "REPEATED", "PROPER"}
MISSED_STATUSES = {
    "NO ANSWER", "FAILED", "BUSY", "NO-FORWARD", "VOICEMAIL",
    "WRONG EXTENSION", "NO EXTENSION", "CLIENT NO ANSWER",
    "FAILED FORBIDDEN DESTINATION", "DECLINE",
}

def norm_status(raw):
    s = (raw or "").upper().replace("+", " ").replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def call_status(raw):
    s = norm_status(raw)
    if s in ANSWERED_STATUSES:
        return "answered"
    if s in MISSED_STATUSES:
        return "missed"
    return "other"

def norm_phone(raw):
    """Останні 10 цифр номера — щоб зрівняти +380xxxxxxxxx / 380xxxxxxxxx /
    0xxxxxxxxx як один і той самий номер. SIP-логіни (нецифрові) сюди не
    потрапляють — для них порівнюємо рядок як є."""
    digits = re.sub(r"\D", "", raw or "")
    return digits[-10:] if len(digits) >= 10 else digits

# ── Довідник співробітників і їх внутрішніх номерів/SIP-логінів ────────────
def fetch_staff():
    """Повертає (staff_names: {staff_id: fio}, directions_exact: {raw_value:
    staff_id}, directions_phone: {norm_phone: staff_id}). staff_names тепер
    використовується для прив'язки дзвінок→менеджер (через employee_number).
    directions_exact/directions_phone лишились лише для живого "хто на лінії
    зараз" (звірка SIP-логінів з /api/ringostat/status)."""
    if not AUTH_KEY:
        raise RuntimeError("RINGOSTAT_AUTH_KEY не задано")
    body = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getProjectStaffListAndDirections",
        "params": {"projectId": PROJECT_ID},
    }
    resp = requests.post(f"{API_BASE}/api/json-rpc", json=body, headers=HTTP_HEADERS, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    if "error" in payload:
        raise RuntimeError(f"Ringostat staff API: {payload['error']}")

    result = payload.get("result", payload)
    entries = []
    if isinstance(result, list):
        entries = result
    elif isinstance(result, dict):
        for key in ("staff", "employees", "items", "list"):
            if isinstance(result.get(key), list):
                entries = result[key]
                break
        if not entries and result and all(isinstance(v, dict) for v in result.values()):
            entries = list(result.values())

    staff_names, directions_exact, directions_phone = {}, {}, {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        staff_id = str(e.get("staffId") or e.get("id") or e.get("ID") or "")
        if not staff_id:
            continue
        fio = e.get("fio") or e.get("name") or e.get("email") or f"ID {staff_id}"
        staff_names[staff_id] = fio

        raw_values = []
        ext = e.get("extensionNumber")
        if ext:
            raw_values.append(str(ext))
        for group_key in ("main", "additional", "directions"):
            grp = e.get(group_key)
            if isinstance(grp, list):
                for d in grp:
                    if isinstance(d, dict):
                        val = d.get("direction") or d.get("value") or d.get("number")
                        if val:
                            raw_values.append(str(val))
                    elif d:
                        raw_values.append(str(d))
            elif isinstance(grp, dict):
                val = grp.get("direction")
                if val:
                    raw_values.append(str(val))

        for v in raw_values:
            directions_exact[v] = staff_id
            p = norm_phone(v)
            if p:
                directions_phone[p] = staff_id

    return staff_names, directions_exact, directions_phone

# ── Дзвінки (Call log) ───────────────────────────────────────────────────────
# employee_number/employee_fio/call_type — офіційні поля з довідника
# параметрів Ringostat calls/list, саме вони дають правильну прив'язку
# дзвінка до менеджера (а не caller/dst, які є номером клієнта й лінії
# відстеження). CALL_FIELDS_FALLBACK — про всяк випадок, якщо якийсь дуже
# старий проєкт Ringostat їх ще не підтримує.
# pool_name і referrer — звідси зрозуміло, З ЯКОГО САЙТУ дзвінок. Сайтів у
# проєкті Ringostat кілька (matroluxe.ua, matroluxe.com, simplershop), і без
# цих полів усі дзвінки виглядають однаково.
#
# Чому не page. У вигрузці звіту є стовпець «Сторінка дзвінка» (page), і
# саме в ньому сайт видно завжди. Але в API такого поля немає: запит із
# ним повертає "You may have entered an incorrect field name" — причому
# НА ВЕСЬ запит, разом із правильними полями поруч. Через це збір мовчки
# відкочувався на бідніший набір, і сайт був порожній у кожного дзвінка.
# Тому тут лишаються тільки ті імена, існування яких перевірено
# маршрутом /api/ringostat/probe.
CALL_FIELDS = ("calldate,caller,dst,disposition,billsec,utm_source,utm_medium,"
               "call_type,employee_number,employee_fio,pool_name,referrer")
CALL_FIELDS_MID = ("calldate,caller,dst,disposition,billsec,utm_source,utm_medium,"
                   "call_type,employee_number,employee_fio")
CALL_FIELDS_FALLBACK = "calldate,caller,dst,disposition,billsec,utm_source,utm_medium"

# Назва сайту за пулом або адресою сторінки. Порядок має значення: «.com»
# перевіряємо раніше за «.ua», інакше matroluxe.com впіймається як matroluxe.ua
# на адресах, де є обидва рядки.
SITE_RULES = (
    ("simplershop",   "simplershop"),
    ("matroluxe.com", "matroluxe.com"),
    ("matro-roll",    "matro-roll.ua"),
    ("matroluxe.ua",  "matroluxe.ua"),
)


def _match_site(text):
    for needle, name in SITE_RULES:
        if needle in text:
            return name
    return ""


def call_site(r):
    """Сайт дзвінка: спершу пул номера, потім реферер.

    Пул — ознака надійна: номер належить конкретному сайту. Але заявки на
    зворотний дзвінок з усіх сайтів лягають у спільний пул «Callback», і
    там пул не каже нічого. Для них лишається реферер — сторінка, з якої
    людина прийшла. Він буває зовнішнім (facebook.com, google.com), тоді
    сайт так і лишається невідомим; зате коли людина переходила всередині
    сайту, домен у реферері — саме той, що потрібен.

    Порядок має значення: пул завжди переважає над реферером, інакше
    перехід із simplershop на matroluxe зарахувався б не тому сайту."""
    by_pool = _match_site(str(r.get("pool_name") or "").lower())
    if by_pool:
        return by_pool
    return _match_site(str(r.get("referrer") or "").lower())


def _host(url):
    """Домен адреси. Порівнюємо саме його, а не весь рядок: у query-параметрах
    трапляються чужі домени (gad_source, utm_content із назвою сайту), і
    пошук підрядка по всій адресі віддавав би не той магазин."""
    u = str(url or "").strip()
    if not u or u == "/":
        return ""
    if "://" not in u:
        u = "http://" + u
    try:
        return (urlparse(u).hostname or "").lower()
    except Exception:
        return ""


# ── Сайт дзвінка з вигрузки Ringostat у Google Sheets ──────────────────────
#
# Навіщо це взагалі. Сайт дзвінка ми досі брали з пулу номера, і це
# виявилось просто неправильно. Звірка з вигрузкою за 26.06—25.11.2026
# (16 078 дзвінків) показала: із 332 дзвінків із пулом «simplershop» на
# simplershop реально прийшли 74, ще 175 — на matroluxe.ua і 68 — на
# matroluxe.com. А 2 122 справжніх дзвінки з simplershop пулу не мали
# зовсім. Тобто пул і недобирає, і приписує чужі дзвінки не тому магазину.
#
# У вигрузці є стовпець «Сторінка дзвінка» — адреса, з якої дзвонили, і
# вона є майже завжди. В API такого поля немає: запит із ним повертає
# "You may have entered an incorrect field name" НА ВЕСЬ запит (див.
# коментар біля CALL_FIELDS). Тому єдиний шлях до нього — та сама
# вигрузка, яку Ringostat сам щодня дописує в Google Sheets.
#
# Телефони з таблиці нікуди не зберігаються: одразу перетворюються на той
# самий хеш, що й у решті проєкту (phone_key.py).
SHEET_COLS = ("calldate", "caller", "page", "pool_name")


def load_sheet_sites(since=""):
    """{(день, хеш номера): сайт} з CSV-вигрузки. Порожньо, якщо не задано.

    since — не старші за цю дату рядки. Ringostat дописує вигрузку щодня й
    нічого з неї не видаляє, тож із часом це десятки тисяч рядків, тоді як
    дашборду потрібні лише останні DAYS_BACK днів. Без відсікання збір
    щоразу перемелював би всю історію заради того самого вікна."""
    if not SHEET_CSV_URL:
        return {}, {"used": False}

    r = requests.get(SHEET_CSV_URL, timeout=120, allow_redirects=True)
    r.raise_for_status()
    text = r.content.decode("utf-8-sig", errors="replace")
    # Google на закритий файл віддає сторінку входу, причому зі статусом 200.
    # Без цієї перевірки csv мовчки розбере HTML і поверне нуль рядків.
    if text.lstrip()[:1] == "<":
        raise RuntimeError("замість CSV прийшов HTML — у файлу вимкнено доступ "
                           "за посиланням або посилання не на export?format=csv")

    rows = list(csv.reader(io.StringIO(text)))
    head = next((i for i, row in enumerate(rows)
                 if "calldate" in [c.strip() for c in row]), -1)
    if head < 0:
        raise RuntimeError("у вигрузці немає рядка заголовків зі стовпцем calldate")
    names = [c.strip() for c in rows[head]]
    idx = {}
    for col in SHEET_COLS:
        if col not in names:
            raise RuntimeError(f"у вигрузці немає стовпця «{col}»")
        idx[col] = names.index(col)

    out = {}
    stat = {"used": True, "total": len(rows) - head - 1, "since": since,
            "rows": 0, "by_page": 0, "by_pool": 0, "no_site": 0}
    for row in rows[head + 1:]:
        if len(row) <= max(idx.values()):
            continue
        calldate = row[idx["calldate"]].strip()
        # Другий рядок шапки — ті самі стовпці українською; у нього в
        # calldate текст, а не дата, тому він відсіюється сам.
        if len(calldate) < 10 or not calldate[:4].isdigit():
            continue
        if since and calldate[:10] < since:
            continue
        key = phone_key(row[idx["caller"]])
        if not key:
            continue
        stat["rows"] += 1
        site = _match_site(_host(row[idx["page"]]))
        if site:
            stat["by_page"] += 1
        else:
            site = _match_site(row[idx["pool_name"]].strip().lower())
            if site:
                stat["by_pool"] += 1
            else:
                stat["no_site"] += 1
                continue
        # Один номер за день може дзвонити з двох сайтів. Беремо перший
        # дзвінок дня: він показує, звідки людина прийшла, а наступні — це
        # вже повтори по тому самому зверненню.
        out.setdefault((calldate[:10], key), site)
    return out, stat

CALL_TYPE_DIRECTION = {
    "in": "incoming",
    "transitin": "incoming",
    "out": "outgoing",
    "callback": "outgoing",
    "transitout": "outgoing",
}

def _fetch_calls_window_with_fields(dt_from, dt_to, fields, tries=3):
    params = {
        "token": AUTH_KEY,          # деякі версії API беруть ключ параметром...
        "project_id": PROJECT_ID,
        "export_type": "json",
        "from": dt_from.strftime("%Y-%m-%d %H:%M:%S"),
        "to": dt_to.strftime("%Y-%m-%d %H:%M:%S"),
        "fields": fields,
    }
    last_err = None
    for attempt in range(tries):
        try:
            resp = requests.get(f"{API_BASE}/calls/list", params=params, headers=HTTP_HEADERS, timeout=45)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and "error" in data:
                raise RuntimeError(str(data["error"]))
            if isinstance(data, dict):
                for key in ("result", "calls", "data", "items"):
                    if isinstance(data.get(key), list):
                        return data[key]
                return []
            if isinstance(data, list):
                return data
            return []
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Ringostat calls/list {dt_from.date()}—{dt_to.date()}: {last_err}")

def fetch_calls_window(dt_from, dt_to):
    # Три спроби, від найповнішого набору до найбіднішого: якщо проєкт не
    # знає pool_name/page — втрачаємо лише прив'язку до сайту, а не весь збір.
    attempts = ((CALL_FIELDS, "без pool_name/page (сайт дзвінка буде невідомий)"),
                (CALL_FIELDS_MID, "без employee_number/call_type"),
                (CALL_FIELDS_FALLBACK, None))
    for fields, nxt in attempts:
        try:
            return _fetch_calls_window_with_fields(dt_from, dt_to, fields)
        except Exception as e:
            last = e
            print(f"  ⚠ набір полів не спрацював ({e})"
                  + (f", пробую {nxt}" if nxt else ""))
    raise last

def fetch_all_calls(days_back):
    today = datetime.now()
    start = today - timedelta(days=days_back)
    out = []
    cur = start
    while cur < today:
        window_end = min(cur + timedelta(days=CHUNK_DAYS), today)
        try:
            rows = fetch_calls_window(cur, window_end)
            out.extend(rows)
        except Exception as e:
            print(f"  ⚠ вікно {cur.date()}—{window_end.date()} не вдалося: {e}")
        cur = window_end
    return out

# ── Перетворення в компактні "сирі" рядки для фронтенду ─────────────────────
def slim_calls(rows, known_staff_names, sheet_sites=None):
    """known_staff_names — {staff_id: fio} з fetch_staff(), лише щоб
    визначити, чи employee_number вже відомий, чи це "новий" співробітник,
    якого немає в getProjectStaffListAndDirections (тоді беремо ім'я прямо
    з employee_fio дзвінка).

    sheet_sites — {(день, хеш номера): сайт} з вигрузки. Має пріоритет над
    пулом: у вигрузці сайт узятий з адреси сторінки, а пул показує не той
    магазин у трьох випадках із чотирьох."""
    sheet_sites = sheet_sites or {}
    out = []
    extra_names = {}
    no_employee = 0
    src = {"sheet": 0, "api": 0, "none": 0}
    for r in rows:
        if not isinstance(r, dict):
            continue
        calldate = r.get("calldate") or r.get("call_date") or r.get("date") or ""
        d = calldate[:10] if calldate else None
        disposition = r.get("disposition") or ""

        staff_id = str(r.get("employee_number") or "")
        if staff_id and staff_id not in known_staff_names and staff_id not in extra_names:
            fio = r.get("employee_fio")
            if fio:
                extra_names[staff_id] = fio
        if not staff_id:
            no_employee += 1

        call_type = (r.get("call_type") or "").strip().lower()
        direction = CALL_TYPE_DIRECTION.get(call_type, "unknown")

        caller_key = phone_key(r.get("caller"))
        site = sheet_sites.get((d, caller_key), "")
        if site:
            src["sheet"] += 1
        else:
            site = call_site(r)
            src["api" if site else "none"] += 1

        out.append({
            "date": d,
            # Точний час і ключ номера потрібні модулю балів: за ними
            # відкидаються дублі (той самий дзвінок приходить у журналі
            # 2–3 рази) і шукається угода того самого клієнта.
            "ts": calldate,
            "caller_key": caller_key,
            # Сам номер, реферер і пул не зберігаємо — тільки назву сайту:
            # у реферері прилітають рекламні мітки й ідентифікатори
            # переходу, яким у файлі даних робити нічого.
            "site": site,
            "direction": direction,
            "status": call_status(disposition),
            "manager_id": staff_id,
            "duration": int(r.get("billsec") or 0),
        })

    # Дедуплікація: один дзвінок = один (час, номер, напрямок). У журналі
    # Ringostat той самий дзвінок трапляється кілька разів, і без цього
    # «вхідних» виходило б удвічі-втричі більше, ніж було насправді.
    seen = set()
    deduped = []
    for c in out:
        key = (c["ts"], c["caller_key"], c["direction"])
        if c["ts"] and key in seen:
            continue
        seen.add(key)
        deduped.append(c)
    dropped = len(out) - len(deduped)
    if dropped:
        print(f"     дублів у журналі відкинуто: {dropped} з {len(out)}")
    return deduped, extra_names, no_employee, src

# Швидкий збір: лише останні дні, решта журналу береться з попереднього
# файлу. 90 днів тягнуться тижневими вікнами — це тринадцять запитів і
# хвилини очікування, а щоб побачити сьогоднішній дзвінок, досить одного.
QUICK_DAYS = 0
for _i, _a in enumerate(sys.argv):
    if _a == "--days" and _i + 1 < len(sys.argv):
        try: QUICK_DAYS = max(1, int(sys.argv[_i + 1]))
        except ValueError: QUICK_DAYS = 0


# ── Main ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== Ringostat Calls Fetch ===")
    if not AUTH_KEY:
        print("⚠  Задайте RINGOSTAT_AUTH_KEY (Render Environment).")
        sys.exit(1)

    # З часом, а не лише з датою: сторінка показує вік даних, і «сьогодні»
    # без годин не дає зрозуміти, збиралось це десять хвилин чи десять
    # годин тому.
    result = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
              "period_from": str(date.today() - timedelta(days=DAYS_BACK))}
    # Доповнювати нема чого, якщо файлу ще немає (після деплою диск чистий):
    # тоді «оновити лише сьогодні» лишило б журнал з одним днем.
    quick = QUICK_DAYS
    prev_calls = []
    if quick:
        try:
            prev = json.loads(OUT.read_text(encoding="utf-8"))
            prev_calls = prev.get("calls") or []
            result["period_from"] = prev.get("period_from") or result["period_from"]
        except Exception:
            prev_calls = []
        if not prev_calls:
            print("  ⚠ попереднього журналу немає — швидкий збір пропущено, беру весь період")
            quick = 0

    days_back = (quick - 1) if quick else DAYS_BACK
    ok = True
    staff_names, directions_exact, directions_phone = {}, {}, {}
    try:
        print("  → Довідник співробітників і їх номерів")
        staff_names, directions_exact, directions_phone = fetch_staff()
        print(f"     знайдено співробітників: {len(staff_names)}")
    except Exception as e:
        print(f"  ✗ ошибка (staff): {e}")
        ok = False

    # Вигрузку тягнемо до дзвінків: без неї сайт доведеться брати з пулу,
    # і про це треба сказати в логу одразу, а не після збору.
    sheet_sites, sheet_stat = {}, {"used": False}
    try:
        sheet_sites, sheet_stat = load_sheet_sites(result["period_from"])
        if sheet_stat.get("used"):
            print(f"  → Вигрузка Ringostat (сайт дзвінка): {sheet_stat['rows']} рядків "
                  f"з {sheet_stat['total']} (з {sheet_stat['since']}), "
                  f"сайт зі сторінки {sheet_stat['by_page']}, з пулу {sheet_stat['by_pool']}, "
                  f"без сайту {sheet_stat['no_site']}")
        else:
            print("  → RINGOSTAT_SHEET_CSV не задано — сайт дзвінка беремо з пулу "
                  "(сильно занижує «Дзвінки» в таблиці магазинів)")
    except Exception as e:
        print(f"  ⚠ вигрузка не завантажилась ({e}) — сайт дзвінка беремо з пулу")
        sheet_stat = {"used": False, "error": str(e)[:200]}

    try:
        print(f"  → Дзвінки за останні {days_back} днів" + (" (швидкий збір)" if quick else ""))
        raw_calls = fetch_all_calls(days_back)
        print(f"     знайдено: {len(raw_calls)}")
        calls, extra_names, no_employee, site_src = slim_calls(raw_calls, staff_names, sheet_sites)
        print(f"     сайт дзвінка: з вигрузки {site_src['sheet']}, "
              f"з пулу/реферера {site_src['api']}, невідомий {site_src['none']}")
        if quick:
            # Старі дзвінки лишаємо, всередині вікна беремо свіжі.
            start = str(date.today() - timedelta(days=days_back))
            old = [c for c in prev_calls if (c.get("date") or "") < start]
            print(f"     склеюю: {len(old)} старих + {len(calls)} свіжих")
            calls = old + calls
        result["calls"] = calls
        if extra_names:
            staff_names.update(extra_names)
            print(f"     довідник доповнено {len(extra_names)} іменами прямо з дзвінків")
        if calls:
            print(f"     без прив'язки до менеджера: {no_employee} з {len(calls)}")
    except Exception as e:
        print(f"  ✗ ошибка (calls): {e}")
        ok = False
        result["calls"] = []
        site_src = {"sheet": 0, "api": 0, "none": 0}

    # directions_exact віддаємо у meta — ringostat.html/home.html зіставляють
    # ними SIP-логіни з /api/ringostat/status (живий "хто зараз на лінії") з
    # конкретним менеджером, без повторного звернення до Ringostat.
    result["meta"] = {"staff": staff_names, "directions": directions_exact,
                      # Звідки взявся сайт дзвінка. Потрібно, щоб мовчазний
                      # відкат на пул було видно: цифри в «Дзвінках» при
                      # цьому не зникають, вони просто стають утричі меншими.
                      "site_source": site_src, "sheet": sheet_stat}

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("calls"):
        print(f"\n✓ ringostat_data.json сохранён: {len(result['calls'])} дзвінків")
    else:
        print("\n⚠ ringostat_data.json сохранён порожнім/частково")

    if not ok:
        sys.exit(1)
