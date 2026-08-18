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

ПРИМІТКА для наступного, хто буде це підтримувати: точні назви полів у
відповіді Ringostat на getProjectStaffListAndDirections документовані не
до кінця однозначно (ми спираємось на офіційний опис полів, а не на
перевірений живий приклад — ключа для тестового виклику під час розробки
не було). Тому tie-матчинг дзвінків до менеджера навмисно захищений
try/except і рахує "unmatched" — при першому реальному запуску варто
глянути лог Render: якщо unmatched велике, треба буде звірити реальні
назви полів у відповіді (там же в лозі є приклад сирого staff-запису).
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
    staff_id}, directions_phone: {norm_phone: staff_id}) — щоб потім зв'язати
    дзвінок (по caller/dst) з конкретним менеджером."""
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

    if entries:
        # [debug] тимчасовий діагностичний дамп — щоб звірити реальні назви
        # полів Ringostat з тим, що очікує парсер нижче. Прибрати після того,
        # як зіставлення дзвінок→менеджер запрацює нормально.
        print("  [debug] сирий запис співробітника (перший з {}):" .format(len(entries)))
        print(json.dumps(entries[0], ensure_ascii=False, indent=2)[:3000])

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

    # [debug] тимчасово — скільки напрямків вдалося витягти і приклад
    print(f"  [debug] directions_exact зібрано: {len(directions_exact)} значень; приклад: {dict(list(directions_exact.items())[:10])}")

    return staff_names, directions_exact, directions_phone

# ── Дзвінки (Call log) ───────────────────────────────────────────────────────
CALL_FIELDS = "calldate,caller,dst,disposition,billsec,utm_source,utm_medium"

def fetch_calls_window(dt_from, dt_to, tries=3):
    params = {
        "token": AUTH_KEY,          # деякі версії API беруть ключ параметром...
        "project_id": PROJECT_ID,
        "export_type": "json",
        "from": dt_from.strftime("%Y-%m-%d %H:%M:%S"),
        "to": dt_to.strftime("%Y-%m-%d %H:%M:%S"),
        "fields": CALL_FIELDS,
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
def slim_calls(rows, directions_exact, directions_phone):
    out = []
    unmatched = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        calldate = r.get("calldate") or r.get("call_date") or r.get("date") or ""
        d = calldate[:10] if calldate else None
        caller = str(r.get("caller") or "")
        dst = str(r.get("dst") or "")
        disposition = r.get("disposition") or ""

        staff_id = directions_exact.get(caller) or directions_exact.get(dst)
        direction = None
        if directions_exact.get(caller):
            direction = "outgoing"
        elif directions_exact.get(dst):
            direction = "incoming"
        if not staff_id:
            staff_id = directions_phone.get(norm_phone(caller))
            if staff_id:
                direction = "outgoing"
            else:
                staff_id = directions_phone.get(norm_phone(dst))
                if staff_id:
                    direction = "incoming"
        if not staff_id:
            unmatched += 1

        out.append({
            "date": d,
            "direction": direction or "unknown",
            "status": call_status(disposition),
            "manager_id": staff_id or "",
            "duration": int(r.get("billsec") or 0),
        })
    return out, unmatched

# ── Main ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== Ringostat Calls Fetch ===")
    if not AUTH_KEY:
        print("⚠  Задайте RINGOSTAT_AUTH_KEY (Render Environment).")
        sys.exit(1)

    result = {"generated_at": str(date.today()), "period_from": str(date.today() - timedelta(days=DAYS_BACK))}
    ok = True
    try:
        print("  → Довідник співробітників і їх номерів")
        staff_names, directions_exact, directions_phone = fetch_staff()
        print(f"     знайдено співробітників: {len(staff_names)}")
        # directions_exact віддаємо у meta теж — ringostat.html зіставляє ними
        # SIP-логіни з /api/ringostat/status (живий "хто зараз на лінії") з
        # конкретним менеджером, без повторного звернення до Ringostat.
        result["meta"] = {"staff": staff_names, "directions": directions_exact}
    except Exception as e:
        print(f"  ✗ ошибка (staff): {e}")
        ok = False
        staff_names, directions_exact, directions_phone = {}, {}, {}
        result["meta"] = {"staff": {}}

    try:
        print(f"  → Дзвінки за останні {DAYS_BACK} днів")
        raw_calls = fetch_all_calls(DAYS_BACK)
        print(f"     знайдено: {len(raw_calls)}")
        if raw_calls:
            # [debug] тимчасовий діагностичний дамп — прибрати після фіксу зіставлення
            print("  [debug] сирі записи дзвінків (перші 3):")
            for row in raw_calls[:3]:
                print(json.dumps(row, ensure_ascii=False))
        calls, unmatched = slim_calls(raw_calls, directions_exact, directions_phone)
        result["calls"] = calls
        if calls:
            print(f"     не вдалося зв'язати з менеджером: {unmatched} з {len(calls)}")
    except Exception as e:
        print(f"  ✗ ошибка (calls): {e}")
        ok = False
        result["calls"] = []

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("calls"):
        print(f"\n✓ ringostat_data.json сохранён: {len(result['calls'])} дзвінків")
    else:
        print("\n⚠ ringostat_data.json сохранён порожнім/частково")

    if not ok:
        sys.exit(1)
