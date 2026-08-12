"""
fetch_bitrix.py — тянет Угоди (Deals), Ліди (Leads) та прострочені задачі
з Bitrix24 REST API через вхідний вебхук і зберігає bitrix_data.json.
Запускається через GitHub Actions за тим самим принципом, що й fetch_meta.py / fetch_gsc.py.
"""
import json, os, sys
from collections import defaultdict
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
UTM_TOP_N = 40  # скільки рядків UTM-аналітики зберігати (сортовано за сумою)

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

# ── Довідники (стадії угод, статуси лідів, джерела) ─────────────────────────
def fetch_deal_stages():
    """Повертає {stage_id: {"name":..., "sort":..., "category_id":..., "semantics":...}}.
    semantics: 'S' (успішна), 'F' (провалена/забракована), 'P' (в роботі) — це
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

# ── Угоди (Deals) ────────────────────────────────────────────────────────────
DEAL_FIELDS = ["ID", "TITLE", "STAGE_ID", "CATEGORY_ID", "OPPORTUNITY", "CURRENCY_ID",
               "DATE_CREATE", "CLOSEDATE", "CLOSED", "SOURCE_ID", "ASSIGNED_BY_ID",
               "UTM_SOURCE", "UTM_MEDIUM", "UTM_CAMPAIGN", "UTM_CONTENT", "UTM_TERM"]

def fetch_deals(start_date):
    rows = call("crm.deal.list", {
        "filter[>=DATE_CREATE]": start_date.isoformat(),
        "select": DEAL_FIELDS,
        "order[DATE_CREATE]": "ASC",
    })
    return rows

# ── Ліди (Leads) ──────────────────────────────────────────────────────────
LEAD_FIELDS = ["ID", "TITLE", "STATUS_ID", "SOURCE_ID", "OPPORTUNITY", "DATE_CREATE", "STATUS_SEMANTIC_ID",
               "UTM_SOURCE", "UTM_MEDIUM", "UTM_CAMPAIGN", "UTM_CONTENT", "UTM_TERM"]

def fetch_leads(start_date):
    rows = call("crm.lead.list", {
        "filter[>=DATE_CREATE]": start_date.isoformat(),
        "select": LEAD_FIELDS,
        "order[DATE_CREATE]": "ASC",
    })
    return rows

# ── Прострочені задачі ────────────────────────────────────────────────────
def fetch_overdue_tasks():
    """Задачі з дедлайном у минулому, які не позначені виконаними.
    Потребує scope 'task' у вебхука — якщо його нема, Bitrix поверне помилку
    доступу; викликач (main) ловить це окремо і не валить весь скрипт."""
    now_iso = datetime.now().isoformat()
    rows = call("tasks.task.list", {
        "filter[<DEADLINE]": now_iso,
        "filter[!DEADLINE]": "",       # виключає задачі без дедлайну взагалі
        "filter[!STATUS]": 5,          # 5 = Завершена
        "select": ["ID", "TITLE", "DEADLINE", "STATUS", "RESPONSIBLE_ID", "GROUP_ID"],
    }, result_key="tasks")
    return rows

def build_overdue_summary(tasks):
    today = date.today()
    items = []
    for t in tasks:
        if str(t.get("STATUS")) == "5":  # 5 = Завершена — підстраховка на випадок, якщо фільтр API не спрацював
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
        items.append({
            "id": t.get("ID"), "title": t.get("TITLE"), "deadline": deadline_str[:10],
            "days_overdue": days_overdue, "responsible_id": t.get("RESPONSIBLE_ID"),
        })
    items.sort(key=lambda x: -x["days_overdue"])
    return items

# ── UTM-аналітика (спільна для лідів і угод) ────────────────────────────────
def build_utm_summary(items, amount_field="OPPORTUNITY", top_n=UTM_TOP_N):
    groups = defaultdict(lambda: {"count": 0, "sum": 0.0})
    for it in items:
        source = (it.get("UTM_SOURCE") or "").strip() or "(не вказано)"
        campaign = (it.get("UTM_CAMPAIGN") or "").strip() or "(не вказано)"
        medium = (it.get("UTM_MEDIUM") or "").strip()
        key = (source, campaign, medium)
        g = groups[key]
        g["count"] += 1
        g["sum"] += float(it.get(amount_field) or 0)
    result = []
    for (source, campaign, medium), g in groups.items():
        result.append({
            "source": source, "campaign": campaign, "medium": medium,
            "count": g["count"], "sum": round(g["sum"], 2),
        })
    result.sort(key=lambda x: (-x["sum"], -x["count"]))
    return result[:top_n]

# ── Агрегація ────────────────────────────────────────────────────────────────
def to_date(dt_str):
    # Bitrix віддає ISO-8601 з таймзоною, напр. "2026-08-01T12:34:56+03:00"
    return dt_str[:10] if dt_str else None

def build_deals_summary(deals, stage_names):
    daily = defaultdict(lambda: {"count": 0, "sum": 0.0})
    by_stage = defaultdict(lambda: {"count": 0, "sum": 0.0})
    won_count, won_sum = 0, 0.0
    lost_count, lost_sum = 0, 0.0
    for d in deals:
        day = to_date(d.get("DATE_CREATE"))
        amount = float(d.get("OPPORTUNITY") or 0)
        if day:
            b = daily[day]
            b["count"] += 1
            b["sum"] += amount
        stage_id = d.get("STAGE_ID", "")
        sb = by_stage[stage_id]
        sb["count"] += 1
        sb["sum"] += amount
        semantics = stage_names.get(stage_id, {}).get("semantics", "")
        if semantics == "S":
            won_count += 1
            won_sum += amount
        elif semantics == "F":
            lost_count += 1
            lost_sum += amount

    daily_list = [{"date": d, "count": b["count"], "sum": round(b["sum"], 2)} for d, b in sorted(daily.items())]
    stages_list = []
    for stage_id, b in sorted(by_stage.items(), key=lambda x: -x[1]["count"]):
        info = stage_names.get(stage_id, {})
        stages_list.append({
            "stage_id": stage_id,
            "name": info.get("name", stage_id),
            "semantics": info.get("semantics", ""),
            "count": b["count"],
            "sum": round(b["sum"], 2),
        })
    return {
        "total_count": len(deals),
        "total_sum": round(sum(b["sum"] for b in daily.values()), 2),
        "won_count": won_count,
        "won_sum": round(won_sum, 2),
        "lost_count": lost_count,     # "забраковані" угоди — стадія з semantics == 'F'
        "lost_sum": round(lost_sum, 2),
        "daily": daily_list,
        "by_stage": stages_list,
        "utm": build_utm_summary(deals, amount_field="OPPORTUNITY"),
    }

def build_leads_summary(leads, status_names, source_names):
    daily = defaultdict(int)
    by_status = defaultdict(int)
    by_source = defaultdict(int)
    converted = 0
    rejected = 0  # "забраковані" ліди — STATUS_SEMANTIC_ID == 'F' (в Bitrix це, як правило, статус "Некваліфікований"/"JUNK")
    for l in leads:
        day = to_date(l.get("DATE_CREATE"))
        if day:
            daily[day] += 1
        by_status[l.get("STATUS_ID", "")] += 1
        by_source[l.get("SOURCE_ID", "")] += 1
        semantic = l.get("STATUS_SEMANTIC_ID")
        if semantic == "C":
            converted += 1
        elif semantic == "F":
            rejected += 1

    daily_list = [{"date": d, "count": c} for d, c in sorted(daily.items())]
    status_list = sorted(
        [{"status_id": sid, "name": status_names.get(sid, sid), "count": c} for sid, c in by_status.items()],
        key=lambda x: -x["count"])
    source_list = sorted(
        [{"source_id": sid, "name": source_names.get(sid, sid or "(не вказано)"), "count": c} for sid, c in by_source.items()],
        key=lambda x: -x["count"])
    return {
        "total_count": len(leads),
        "converted_count": converted,
        "conversion_rate": round(converted / len(leads) * 100, 1) if leads else 0,
        "rejected_count": rejected,
        "rejected_rate": round(rejected / len(leads) * 100, 1) if leads else 0,
        "daily": daily_list,
        "by_status": status_list,
        "by_source": source_list,
        "utm": build_utm_summary(leads, amount_field="OPPORTUNITY"),
    }

# ── Main ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== Bitrix24 CRM Fetch ===")
    if not WEBHOOK_URL:
        print("⚠  Задайте BITRIX_WEBHOOK_URL (GitHub Secret).")
        sys.exit(1)

    start_date = date.today() - timedelta(days=DAYS_BACK)
    ok = True
    result = {"generated_at": str(date.today()), "period_from": str(start_date)}
    try:
        print("  → Довідники (стадії угод, статуси й джерела лідів)")
        stage_names = fetch_deal_stages()
        status_names = fetch_lead_statuses()
        source_names = fetch_lead_sources()

        print(f"  → Угоди (Deals) з {start_date}")
        deals = fetch_deals(start_date)
        print(f"     знайдено: {len(deals)}")
        result["deals"] = build_deals_summary(deals, stage_names)

        print(f"  → Ліди (Leads) з {start_date}")
        leads = fetch_leads(start_date)
        print(f"     знайдено: {len(leads)}")
        result["leads"] = build_leads_summary(leads, status_names, source_names)

        print(f"     угод забраковано: {result['deals']['lost_count']}, лідів забраковано: {result['leads']['rejected_count']}")
    except Exception as e:
        print(f"  ✗ ошибка (deals/leads): {e}")
        ok = False

    try:
        print("  → Прострочені задачі")
        overdue_raw = fetch_overdue_tasks()
        overdue = build_overdue_summary(overdue_raw)
        result["overdue_tasks"] = {"count": len(overdue), "items": overdue[:200]}
        print(f"     прострочено: {len(overdue)}")
    except Exception as e:
        # Найчастіша причина — у вебхука немає scope 'task'. Не валимо весь
        # скрипт через це: deals/leads все одно варто зберегти.
        print(f"  ⚠ не вдалося отримати задачі (можливо, вебхуку не вистачає доступу 'task'): {e}")
        result["overdue_tasks"] = {"count": None, "items": [], "error": str(e)}

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    if "deals" in result and "leads" in result:
        print(f"\n✓ bitrix_data.json сохранён: {result['deals']['total_count']} угод, {result['leads']['total_count']} лідів")
    else:
        print("\n⚠ bitrix_data.json сохранён частково (deals/leads не вдалося отримати)")

    if not ok:
        sys.exit(1)
