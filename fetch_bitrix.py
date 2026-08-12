"""
fetch_bitrix.py — тянет Угоди (Deals), Ліди (Leads) та прострочені задачі
з Bitrix24 REST API через вхідний вебхук і зберігає bitrix_data.json.

Формат виводу навмисно "сирий" (по рядку на угоду/лід — так само, як
fetch_meta.py віддає по рядку на (дата, кампанія) в data.json) — це дозволяє
bitrix.html самостійно перерахувати будь-яку розбивку (воронку, менеджерів,
UTM, статуси) під довільно вибраний період, а не тільки під той, що був
зафіксований на момент генерації файлу.

Запускається через GitHub Actions за тим самим принципом, що й
fetch_meta.py / fetch_gsc.py.
"""
import json, os, sys
from datetime import date, datetime, timedelta
from pathlib import Path
import urllib.request
import urllib.parse

# ── Настройки ──────────────────────────────────────────────────────────────
# Вхідний вебхук створюється в Bitrix24: Налаштування → Розробникам → Інше →
# Вхідний вебхук. Дай доступ до розділів "CRM" (crm) ТА "Задачі" (task) —
# без "task" прострочені задачі не потягнуться (fetch_overdue_tasks тоді
# просто поверне порожній список і залогує причину, решта не зламається).
# URL виглядає так: https://<портал>.bitrix24.eu/rest/<user_id>/<webhook_key>/
# Кладеться в GitHub Secret BITRIX_WEBHOOK_URL — у файл не вписувати.
WEBHOOK_URL = os.environ.get("BITRIX_WEBHOOK_URL", "").rstrip("/")

DAYS_BACK = 90  # той самий горизонт, що й у fetch_meta.py (DATE_PRESET=last_90d)

OUT = Path(__file__).parent / "bitrix_data.json"

# ── Низькорівневий виклик REST-методу з пагінацією ──────────────────────────
def call(method, params=None, tries=3, result_key=None):
    """result_key потрібен для методів, де result — не список, а об'єкт
    з вкладеним списком (напр. tasks.task.list повертає {"tasks": [...]})."""
    if not WEBHOOK_URL:
        raise RuntimeError("BITRIX_WEBHOOK_URL не задано")
    params = params or {}
    all_items = []
    start = 0
    while True:
        q = dict(params)
        q["start"] = start
        url = f"{WEBHOOK_URL}/{method}.json?{urllib.parse.urlencode(q, doseq=True)}"
        data = None
        for attempt in range(tries):
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except Exception as e:
                if attempt == tries - 1:
                    raise RuntimeError(f"Bitrix API {method} failed: {e}")
        if "error" in data:
            raise RuntimeError(f"Bitrix API {method}: {data.get('error_description', data['error'])}")
        result = data.get("result", [])
        if result_key:
            result = result.get(result_key, []) if isinstance(result, dict) else []
        all_items.extend(result if isinstance(result, list) else [])
        nxt = data.get("next")
        if nxt is None or not result:
            break
        start = nxt
    return all_items

# ── Довідники (стадії угод, статуси/джерела лідів, користувачі) ────────────
def fetch_deal_stages():
    """{stage_id: {"name":..., "sort":..., "category_id":..., "semantics":...}}.
    semantics: 'S' (успішна), 'F' (провалена/забракована), 'P' (в роботі) —
    офіційне поле Bitrix, надійніше за пошук підрядка "WON"/"LOSE" в STAGE_ID."""
    stages = {}
    categories = call("crm.dealcategory.list")
    cat_ids = [0] + [int(c["ID"]) for c in categories]  # 0 — стандартна воронка
    for cat_id in cat_ids:
        try:
            rows = call("crm.dealcategory.stage.list", {"id": cat_id})
        except Exception:
            continue
        for r in rows:
            stages[r["STATUS_ID"]] = {
                "name": r["NAME"], "sort": int(r.get("SORT", 0)),
                "category_id": cat_id, "semantics": r.get("SEMANTICS", ""),
            }
    return stages

def fetch_lead_statuses():
    rows = call("crm.status.list", {"filter[ENTITY_ID]": "STATUS"})
    return {r["STATUS_ID"]: r["NAME"] for r in rows}

def fetch_lead_sources():
    rows = call("crm.status.list", {"filter[ENTITY_ID]": "SOURCE"})
    return {r["STATUS_ID"]: r["NAME"] for r in rows}

def fetch_users():
    """{user_id: "Ім'я Прізвище"} — для підпису менеджерів у сделках/лідах/задачах."""
    rows = call("user.get", {"ACTIVE": "true"})
    out = {}
    for u in rows:
        name = f"{u.get('NAME','')} {u.get('LAST_NAME','')}".strip()
        out[str(u["ID"])] = name or u.get("EMAIL") or f"ID {u['ID']}"
    return out

# ── Угоди (Deals) ────────────────────────────────────────────────────────────
DEAL_FIELDS = ["ID", "TITLE", "STAGE_ID", "CATEGORY_ID", "OPPORTUNITY", "CURRENCY_ID",
               "DATE_CREATE", "CLOSEDATE", "CLOSED", "SOURCE_ID", "ASSIGNED_BY_ID",
               "UTM_SOURCE", "UTM_MEDIUM", "UTM_CAMPAIGN", "UTM_CONTENT", "UTM_TERM"]

def fetch_deals(start_date):
    return call("crm.deal.list", {
        "filter[>=DATE_CREATE]": start_date.isoformat(),
        "select": DEAL_FIELDS,
        "order[DATE_CREATE]": "ASC",
    })

# ── Ліди (Leads) ──────────────────────────────────────────────────────────
LEAD_FIELDS = ["ID", "TITLE", "STATUS_ID", "SOURCE_ID", "OPPORTUNITY", "DATE_CREATE",
               "STATUS_SEMANTIC_ID", "ASSIGNED_BY_ID",
               "UTM_SOURCE", "UTM_MEDIUM", "UTM_CAMPAIGN", "UTM_CONTENT", "UTM_TERM"]

def fetch_leads(start_date):
    return call("crm.lead.list", {
        "filter[>=DATE_CREATE]": start_date.isoformat(),
        "select": LEAD_FIELDS,
        "order[DATE_CREATE]": "ASC",
    })

# ── Прострочені задачі ────────────────────────────────────────────────────
def fetch_overdue_tasks():
    """Задачі з дедлайном у минулому, які не позначені виконаними.
    Потребує scope 'task' у вебхука — якщо його нема (чи фільтр не підходить
    під конкретний портал), Bitrix поверне помилку; викликач (main) ловить
    це окремо і не валить весь скрипт."""
    # Без мікросекунд і без "!DEADLINE": "" — саме ця комбінація раніше
    # ламала запит з HTTP 400 на деяких порталах.
    now_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    rows = call("tasks.task.list", {
        "filter[<DEADLINE]": now_str,
        "filter[!STATUS]": 5,  # 5 = Завершена
        "select": ["ID", "TITLE", "DEADLINE", "STATUS", "RESPONSIBLE_ID", "GROUP_ID"],
    }, result_key="tasks")
    return rows

def build_overdue(tasks, user_names):
    today = date.today()
    items = []
    for t in tasks:
        if str(t.get("STATUS")) == "5":  # підстраховка, якщо фільтр API не спрацював
            continue
        deadline_str = t.get("DEADLINE")
        if not deadline_str:
            continue
        try:
            d = date.fromisoformat(deadline_str[:10])
        except ValueError:
            continue
        days_overdue = (today - d).days
        if days_overdue <= 0:
            continue
        rid = str(t.get("RESPONSIBLE_ID") or "")
        items.append({
            "id": t.get("ID"), "title": t.get("TITLE"), "deadline": deadline_str[:10],
            "days_overdue": days_overdue, "responsible_id": rid,
            "responsible_name": user_names.get(rid, rid or "(не призначено)"),
        })
    items.sort(key=lambda x: -x["days_overdue"])
    return items

# ── Перетворення в компактні "сирі" рядки для фронтенду ─────────────────────
def to_date(dt_str):
    return dt_str[:10] if dt_str else None  # Bitrix віддає ISO-8601 з таймзоною

def slim_deals(deals):
    out = []
    for d in deals:
        out.append({
            "id": d.get("ID"),
            "date": to_date(d.get("DATE_CREATE")),
            "stage_id": d.get("STAGE_ID", ""),
            "amount": float(d.get("OPPORTUNITY") or 0),
            "manager_id": str(d.get("ASSIGNED_BY_ID") or ""),
            "utm_source": (d.get("UTM_SOURCE") or "").strip(),
            "utm_campaign": (d.get("UTM_CAMPAIGN") or "").strip(),
            "utm_medium": (d.get("UTM_MEDIUM") or "").strip(),
        })
    return out

def slim_leads(leads):
    out = []
    for l in leads:
        out.append({
            "id": l.get("ID"),
            "date": to_date(l.get("DATE_CREATE")),
            "status_id": l.get("STATUS_ID", ""),
            "source_id": l.get("SOURCE_ID", ""),
            "semantic": l.get("STATUS_SEMANTIC_ID", ""),
            "amount": float(l.get("OPPORTUNITY") or 0),
            "manager_id": str(l.get("ASSIGNED_BY_ID") or ""),
            "utm_source": (l.get("UTM_SOURCE") or "").strip(),
            "utm_campaign": (l.get("UTM_CAMPAIGN") or "").strip(),
            "utm_medium": (l.get("UTM_MEDIUM") or "").strip(),
        })
    return out

# ── Main ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== Bitrix24 CRM Fetch ===")
    if not WEBHOOK_URL:
        print("⚠  Задайте BITRIX_WEBHOOK_URL (GitHub Secret).")
        sys.exit(1)

    start_date = date.today() - timedelta(days=DAYS_BACK)
    ok = True
    result = {"generated_at": str(date.today()), "period_from": str(start_date)}
    user_names = {}
    try:
        print("  → Довідники (стадії угод, статуси/джерела лідів, користувачі)")
        stage_names = fetch_deal_stages()
        status_names = fetch_lead_statuses()
        source_names = fetch_lead_sources()
        user_names = fetch_users()
        result["meta"] = {
            "stages": stage_names,
            "lead_statuses": status_names,
            "lead_sources": source_names,
            "users": user_names,
        }

        print(f"  → Угоди (Deals) з {start_date}")
        deals = fetch_deals(start_date)
        print(f"     знайдено: {len(deals)}")
        result["deals"] = slim_deals(deals)

        print(f"  → Ліди (Leads) з {start_date}")
        leads = fetch_leads(start_date)
        print(f"     знайдено: {len(leads)}")
        result["leads"] = slim_leads(leads)
    except Exception as e:
        print(f"  ✗ ошибка (deals/leads): {e}")
        ok = False

    try:
        print("  → Прострочені задачі")
        overdue_raw = fetch_overdue_tasks()
        overdue = build_overdue(overdue_raw, user_names)
        result["overdue_tasks"] = {"count": len(overdue), "items": overdue[:300]}
        print(f"     прострочено: {len(overdue)}")
    except Exception as e:
        # Найчастіша причина — у вебхука немає scope 'task'. Не валимо весь
        # скрипт через це: deals/leads все одно варто зберегти.
        print(f"  ⚠ не вдалося отримати задачі (можливо, вебхуку не вистачає доступу 'task'): {e}")
        result["overdue_tasks"] = {"count": None, "items": [], "error": str(e)}

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    if "deals" in result and "leads" in result:
        print(f"\n✓ bitrix_data.json сохранён: {len(result['deals'])} угод, {len(result['leads'])} лідів")
    else:
        print("\n⚠ bitrix_data.json сохранён частково (deals/leads не вдалося отримати)")

    if not ok:
        sys.exit(1)
