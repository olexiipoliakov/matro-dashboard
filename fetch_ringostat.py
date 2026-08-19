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
import json, os, re, sys
from datetime import date, datetime, timedelta
from pathlib import Path
import requests

AUTH_KEY = os.environ.get("RINGOSTAT_AUTH_KEY", "")
PROJECT_ID = os.environ.get("RINGOSTAT_PROJECT_ID", "")

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
CALL_FIELDS = "calldate,caller,dst,disposition,billsec,utm_source,utm_medium,call_type,employee_number,employee_fio"
CALL_FIELDS_FALLBACK = "calldate,caller,dst,disposition,billsec,utm_source,utm_medium"

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
    try:
        return _fetch_calls_window_with_fields(dt_from, dt_to, CALL_FIELDS)
    except Exception as e:
        print(f"  ⚠ розширений набір полів не спрацював ({e}), пробую без employee_number/call_type")
        return _fetch_calls_window_with_fields(dt_from, dt_to, CALL_FIELDS_FALLBACK)

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
def slim_calls(rows, known_staff_names):
    """known_staff_names — {staff_id: fio} з fetch_staff(), лише щоб
    визначити, чи employee_number вже відомий, чи це "новий" співробітник,
    якого немає в getProjectStaffListAndDirections (тоді беремо ім'я прямо
    з employee_fio дзвінка)."""
    out = []
    extra_names = {}
    no_employee = 0
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

        out.append({
            "date": d,
            "direction": direction,
            "status": call_status(disposition),
            "manager_id": staff_id,
            "duration": int(r.get("billsec") or 0),
        })
    return out, extra_names, no_employee

# ── Main ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== Ringostat Calls Fetch ===")
    if not AUTH_KEY:
        print("⚠  Задайте RINGOSTAT_AUTH_KEY (Render Environment).")
        sys.exit(1)

    result = {"generated_at": str(date.today()), "period_from": str(date.today() - timedelta(days=DAYS_BACK))}
    ok = True
    staff_names, directions_exact, directions_phone = {}, {}, {}
    try:
        print("  → Довідник співробітників і їх номерів")
        staff_names, directions_exact, directions_phone = fetch_staff()
        print(f"     знайдено співробітників: {len(staff_names)}")
    except Exception as e:
        print(f"  ✗ ошибка (staff): {e}")
        ok = False

    try:
        print(f"  → Дзвінки за останні {DAYS_BACK} днів")
        raw_calls = fetch_all_calls(DAYS_BACK)
        print(f"     знайдено: {len(raw_calls)}")
        calls, extra_names, no_employee = slim_calls(raw_calls, staff_names)
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

    # directions_exact віддаємо у meta — ringostat.html/home.html зіставляють
    # ними SIP-логіни з /api/ringostat/status (живий "хто зараз на лінії") з
    # конкретним менеджером, без повторного звернення до Ringostat.
    result["meta"] = {"staff": staff_names, "directions": directions_exact}

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("calls"):
        print(f"\n✓ ringostat_data.json сохранён: {len(result['calls'])} дзвінків")
    else:
        print("\n⚠ ringostat_data.json сохранён порожнім/частково")

    if not ok:
        sys.exit(1)
