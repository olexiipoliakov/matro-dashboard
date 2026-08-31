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
import json, os, sys, time
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

# Деякі хостинги (в т.ч. Bitrix24.eu) віддають "HTTP Error 403: Forbidden"
# на запити без "браузерного" User-Agent — стандартний urllib.request шле
# "Python-urllib/x.x", який часто банить анти-бот захист. Помічено при
# переїзді з GitHub Actions на Render: той самий вебхук раптом почав
# падати з 403 на всі методи одразу (crm.* і tasks.*).
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MatroDashboardBot/1.0)"}

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
                req = urllib.request.Request(url, headers=HTTP_HEADERS)
                with urllib.request.urlopen(req, timeout=30) as resp:
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
        "select[]": DEAL_FIELDS,
        "order[DATE_CREATE]": "ASC",
    })

# ── Категоризація товарних позицій угод (методика DEAL_*.xls) ──────────────
# Джерело: docs/deal-categorization-methodology.md — ручна методика розбору
# дневного экспорта, перенесена сюди 1:1, щоб дашборд рахував те саме
# автоматично з тих самих даних Bitrix (продуктові рядки угод).

# Крок 1 — виключити угоду ЦІЛКОМ з товарообігу.
# "рекламация" додана поверх методики: по ній товар повертають і гроші
# віддають назад, тож у виручку дня вона потрапляти не має — так само,
# як відмова клієнта чи помилково заведена угода.
EXCLUDED_STAGE_NAMES = {"отказ клиента", "ошибочный", "рекламация"}

SERVICE_KEYWORDS = ["наложк", "наложен", "наклад", "налокж", "достак", "доствк", "3анос",
                     "доставк", "занос", "виніс", "підйом на поверх", "послуга збірки", "збірка в день"]

CATEGORY_RULES_PRODUCT = [
    # порядок — пріоритет з методики (крок 3): перший збіг перемагає
    ("accessories",     "Аксесуари",       ["наматрацник", "наматрасник", "подушк", "ковдра",
                                             "підматрацник", "подматрасник", "чохол-сумка", "чехол-сумка"]),
    ("wardrobes",        "Шафи",           ["шафа", "шкаф", "антресоль", "пенал"]),
    ("case_furniture",   "Корпусні меблі", ["тумба", "передпокій", "прихож", "стіл", "стол"]),
    ("soft_furniture",   "М'яка меблі",    ["ліжко", "кровать", "диван", "подіум", "крісло", "каркас", "комплект"]),
    ("mattresses",       "Матраци",        ["матрац", "матрас", "топпер", "топер", "ортопед", "футон"]),
]
CATEGORY_NAMES_PRODUCT = {k: n for k, n, _ in CATEGORY_RULES_PRODUCT}
CATEGORY_NAMES_PRODUCT["unrecognized"] = "Нерозпізнано"

# крок 4 — "голі" назви моделей матраців (без слова "матрац" у назві).
# Список зростає по ходу розбору файлів — це стартовий набір з методики.
KNOWN_MATTRESS_MODELS = [
    "азалія", "azalia", "leo kokos", "leo", "провансе", "provance", "прованс", "бордо", "bordo",
    "аура кокос", "aura kokos", "трафік", "traffic", "діамант", "diamond", "leeds", "лідс",
    "ретріт", "retreat", "камелія", "camelia", "амор", "amore", "meditation", "медитейшн",
    "light kokos", "лайт кокос",
]

def is_service_line(name):
    """Крок 2 — рядок-послуга (доставка/наложка/занос/підйом/збірка), не товар."""
    n = (name or "").strip().lower()
    return any(k in n for k in SERVICE_KEYWORDS)

def has_sluzhbovka_flag(name):
    """Прапор «службовка» в назві — підсвічуємо, не виключаємо автоматично."""
    return "службов" in (name or "").lower()

def classify_product_category(name):
    """Крок 3 + крок 4: категорія товарної позиції за ключовими словами,
    з фолбеком на список відомих моделей матраців для «голих» назв."""
    n = (name or "").strip().lower()
    for key, _name, keywords in CATEGORY_RULES_PRODUCT:
        if any(k in n for k in keywords):
            return key
    if any(model in n for model in KNOWN_MATTRESS_MODELS):
        return "mattresses"
    return "unrecognized"

def fetch_batch(commands, tries=3):
    """До 50 команд Bitrix REST за один HTTP-запит (batch.json). POST, а не GET —
    щоб не впертися в ліміт довжини URL при десятках угод в одному батчі."""
    if not WEBHOOK_URL:
        raise RuntimeError("BITRIX_WEBHOOK_URL не задано")
    body = {"halt": "0"}
    for k, v in commands.items():
        body[f"cmd[{k}]"] = v
    data_bytes = urllib.parse.urlencode(body).encode("utf-8")
    url = f"{WEBHOOK_URL}/batch.json"
    data = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=data_bytes, method="POST", headers=HTTP_HEADERS)
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except Exception as e:
            if attempt == tries - 1:
                raise RuntimeError(f"Bitrix batch failed: {e}")
    if "error" in data:
        raise RuntimeError(f"Bitrix batch error: {data.get('error_description', data['error'])}")
    return data.get("result", {}).get("result", {}) or {}

def fetch_lead_reasons(lead_ids, chunk_size=50):
    """Причини забракування для списку лідів, батчами через crm.lead.get.

    Чому не одним crm.lead.list: списковий метод на цьому порталі не віддає
    користувацькі поля — ні перелічені поштучно, ні по масці "UF_*"; поле
    приходить порожнім, хоча в картці значення стоїть. crm.lead.get по
    одному ліду віддає його справно (перевірено вручну), тому тягнемо
    саме ним і лише по забракованих лідах, щоб не роздувати час вивантаження.

    Повертає {lead_id: (reason_id, reason_text)}.
    """
    out = {}
    lead_ids = list(lead_ids)
    for i in range(0, len(lead_ids), chunk_size):
        chunk = lead_ids[i:i + chunk_size]
        cmds = {f"l{j}": f"crm.lead.get?id={lid}" for j, lid in enumerate(chunk)}
        try:
            result = fetch_batch(cmds)
        except Exception as e:
            print(f"  ⚠ причини забракування: батч {i//chunk_size + 1} не вдався: {e}", flush=True)
            continue
        for j, lid in enumerate(chunk):
            row = result.get(f"l{j}") or {}
            if isinstance(row, dict):
                out[lid] = (_one(row.get(LEAD_REJECT_REASON_FIELD)),
                            _one(row.get(LEAD_REJECT_REASON_TEXT_FIELD)))
        # Пауза між батчами: забракованих лідів понад тисячу, а портал уже
        # відповідав 503 на щільну серію запитів.
        time.sleep(0.3)
    return out

def fetch_deal_products(deal_ids, chunk_size=25):
    """Товарні рядки для списку угод, батчами по `chunk_size` (ліміт Bitrix —
    50 команд на batch-запит, беремо з запасом). Повертає {deal_id: [rows]}."""
    out = {}
    deal_ids = list(deal_ids)
    for i in range(0, len(deal_ids), chunk_size):
        chunk = deal_ids[i:i + chunk_size]
        cmds = {f"d{j}": f"crm.deal.productrows.get?id={did}" for j, did in enumerate(chunk)}
        result = fetch_batch(cmds)
        for j, did in enumerate(chunk):
            out[did] = result.get(f"d{j}", []) or []
    return out

def build_deal_categories(deals_raw, product_rows_by_id, stage_names):
    """Крок 1-8 методики: для кожної угоди (крім LOSE/Ошибочный) рахує оборот
    по категоріях з її товарних рядків. Повертає сирий список по угодах —
    так само, як deals/leads — щоб bitrix.html міг перезрізати по будь-якому
    періоду на клієнті, а не тільки по тому, що зафіксовано на момент генерації."""
    out = []
    for d in deals_raw:
        stage_id = d.get("STAGE_ID", "")
        stage_name = stage_names.get(stage_id, {}).get("name", "").strip().lower()
        if stage_name in EXCLUDED_STAGE_NAMES:
            continue  # крок 1

        deal_id = d.get("ID")
        rows = product_rows_by_id.get(deal_id, [])
        cat_sums = {"mattresses": 0.0, "soft_furniture": 0.0, "wardrobes": 0.0, "case_furniture": 0.0}
        accessories = 0.0
        unrecognized = 0.0
        turnover = 0.0
        wholesale_amount = 0.0
        flags = {"wholesale": False, "sluzhbovka": False, "empty_product": False}

        for r in rows:
            name = r.get("PRODUCT_NAME", "") or ""
            price = float(r.get("PRICE_BRUTTO") or r.get("PRICE") or 0)
            qty = float(r.get("QUANTITY") or 0)
            amount = price * qty

            if not name.strip() and amount:
                flags["empty_product"] = True  # потребує ручної перевірки — не додаємо в оборот
                continue
            if is_service_line(name):
                continue  # крок 2 — повністю виключено (не товар)
            if has_sluzhbovka_flag(name):
                flags["sluzhbovka"] = True
            if qty >= 15:  # оптова партія — приклади з методики: 44 шт/169k; 31+68+59 шт/361k
                flags["wholesale"] = True
                wholesale_amount += amount

            cat = classify_product_category(name)
            turnover += amount
            if cat == "accessories":
                accessories += amount
            elif cat in cat_sums:
                cat_sums[cat] += amount
            else:
                unrecognized += amount

        out.append({
            "deal_id": deal_id,
            "date": to_date(d.get("DATE_CREATE")),
            "source_id": d.get("SOURCE_ID", ""),
            "turnover": round(turnover, 2),
            "turnover_excl_wholesale": round(turnover - wholesale_amount, 2),
            "categories": {k: round(v, 2) for k, v in cat_sums.items()},
            "accessories": round(accessories, 2),
            "unrecognized": round(unrecognized, 2),
            "flags": flags,
        })
    return out

# ── Ліди (Leads) ──────────────────────────────────────────────────────────
# Причина забракування ліда — користувацьке поле в Bitrix. Їх два:
# основне (список із 18 варіантів, ним і користуються менеджери) і старе
# текстове з майже такою ж назвою. Тягнемо обидва: якщо в списку порожньо,
# пробуємо текстове, щоб не втратити старі ліди.
LEAD_REJECT_REASON_FIELD = "UF_CRM_1720011123359"        # тип enumeration
LEAD_REJECT_REASON_TEXT_FIELD = "UF_CRM_1612946419988"   # тип string (застаріле)

LEAD_FIELDS = ["ID", "TITLE", "STATUS_ID", "SOURCE_ID", "OPPORTUNITY", "DATE_CREATE",
               "STATUS_SEMANTIC_ID", "ASSIGNED_BY_ID",
               "UTM_SOURCE", "UTM_MEDIUM", "UTM_CAMPAIGN", "UTM_CONTENT", "UTM_TERM",
               # Користувацькі поля crm.lead.list віддає ТІЛЬКИ по масці "UF_*".
               # Перелічені поштучно коди (UF_CRM_1720011123359 тощо) він мовчки
               # ігнорує — поле приходить порожнім, хоча в картці значення є.
               # Перевірено на живому ліді: crm.lead.get віддає "490",
               # а crm.lead.get зі списком кодів — порожній рядок.
               "UF_*"]

def fetch_reject_reasons():
    """Довідник причин забракування: {id варіанта: текст}. Живе не в
    crm.status.list, а в описі самого поля ліда, тому питаємо crm.lead.fields."""
    try:
        fields = call("crm.lead.fields") or {}
    except Exception as e:
        print(f"  ⚠ не вдалося прочитати довідник причин забракування: {e}", flush=True)
        return {}
    field = fields.get(LEAD_REJECT_REASON_FIELD) or {}
    return {str(i.get("ID")): (i.get("VALUE") or "").strip()
            for i in (field.get("items") or []) if i.get("ID")}

def fetch_leads(start_date):
    return call("crm.lead.list", {
        "filter[>=DATE_CREATE]": start_date.isoformat(),
        "select[]": LEAD_FIELDS,
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
            # SOURCE_ID уже запитувався у DEAL_FIELDS, але раніше губився тут —
            # без нього фронтенд не міг показати "джерело" для конкретної угоди
            # (тільки UTM, який на більшості угод порожній).
            "source_id": (d.get("SOURCE_ID") or "").strip(),
            "utm_source": (d.get("UTM_SOURCE") or "").strip(),
            "utm_campaign": (d.get("UTM_CAMPAIGN") or "").strip(),
            "utm_medium": (d.get("UTM_MEDIUM") or "").strip(),
        })
    return out

def _one(v):
    """Значення користувацького поля Bitrix → рядок. Множинні поля приходять
    списком, числові — числом, порожні — None/False."""
    if isinstance(v, (list, tuple)):
        v = v[0] if v else ""
    if v in (None, False):
        return ""
    return str(v).strip()


def slim_leads(leads):
    out = []
    for l in leads:
        out.append({
            "id": l.get("ID"),
            # Людський заголовок ліда (напр. ім'я з форми чи "Заявка з сайту") —
            # щоб у списку "Неякісні ліди" на дашборді не показувати голий ID.
            "title": (l.get("TITLE") or "").strip(),
            "date": to_date(l.get("DATE_CREATE")),
            "status_id": l.get("STATUS_ID", ""),
            "source_id": l.get("SOURCE_ID", ""),
            "semantic": l.get("STATUS_SEMANTIC_ID", ""),
            "amount": float(l.get("OPPORTUNITY") or 0),
            "manager_id": str(l.get("ASSIGNED_BY_ID") or ""),
            "utm_source": (l.get("UTM_SOURCE") or "").strip(),
            "utm_campaign": (l.get("UTM_CAMPAIGN") or "").strip(),
            "utm_medium": (l.get("UTM_MEDIUM") or "").strip(),
            # Причина забракування: id варіанта зі списку. Розшифровка —
            # у meta.reject_reasons, щоб не дублювати текст у кожному ліді.
            # Поле може прийти рядком, числом або списком (якщо у Bitrix його
            # колись зробили множинним) — зводимо все до одного рядка.
            "reject_reason": _one(l.get(LEAD_REJECT_REASON_FIELD)),
            "reject_reason_text": _one(l.get(LEAD_REJECT_REASON_TEXT_FIELD)),
        })
    return out

def summarize_deal_products(rows):
    """Стисле людське резюме товарних позицій угоди для показу в списку
    "Угоди" на дашборді (напр. "Матрац Sleep 160×200" або "Матрац Sleep
    160×200 + ще 2 поз."). Пропускає рядки-послуги (доставка/наложка/збірка —
    та сама логіка кроку 2 методики категоризації) та порожні назви."""
    names = []
    seen = set()
    for r in rows:
        name = (r.get("PRODUCT_NAME") or "").strip()
        if not name or name in seen or is_service_line(name):
            continue
        seen.add(name)
        names.append(name)
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return f"{names[0]} + ще {len(names) - 1} поз."

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
        reject_reasons = fetch_reject_reasons()
        print(f"     причин забракування у довіднику: {len(reject_reasons)}")
        result["meta"] = {
            "stages": stage_names,
            "lead_statuses": status_names,
            "lead_sources": source_names,
            "users": user_names,
            "reject_reasons": reject_reasons,
        }

        print(f"  → Угоди (Deals) з {start_date}")
        deals = fetch_deals(start_date)
        print(f"     знайдено: {len(deals)}")
        result["deals"] = slim_deals(deals)

        print(f"  → Ліди (Leads) з {start_date}")
        leads = fetch_leads(start_date)
        print(f"     знайдено: {len(leads)}")
        result["leads"] = slim_leads(leads)

        # Причини забракування доводиться добирати окремо — див. коментар
        # у fetch_lead_reasons. Робимо це тільки для забракованих лідів.
        rejected_ids = [l["id"] for l in result["leads"] if l.get("semantic") == "F"]
        if rejected_ids:
            print(f"  → Причини забракування ({len(rejected_ids)} лідів)")
            reasons = fetch_lead_reasons(rejected_ids)
            for l in result["leads"]:
                got = reasons.get(l["id"])
                if got:
                    l["reject_reason"], l["reject_reason_text"] = got
        with_reason = sum(1 for l in result["leads"] if l.get("reject_reason") or l.get("reject_reason_text"))
        print(f"     з причиною забракування: {with_reason}")
    except Exception as e:
        print(f"  ✗ ошибка (deals/leads): {e}")
        ok = False
        deals = []
        stage_names = {}

    try:
        eligible_ids = [d.get("ID") for d in deals
                        if stage_names.get(d.get("STAGE_ID", ""), {}).get("name", "").strip().lower() not in EXCLUDED_STAGE_NAMES]
        print(f"  → Товарні позиції угод ({len(eligible_ids)} угод, категоризація за методикою)")
        product_rows = fetch_deal_products(eligible_ids)
        deal_categories = build_deal_categories(deals, product_rows, stage_names)
        result["deal_categories"] = deal_categories
        print(f"     категоризовано: {len(deal_categories)} угод")

        # Причепляємо людське резюме товару до вже готового result["deals"] —
        # потрібно для списку "Угоди" в деталях менеджера на bitrix.html.
        product_summary_by_id = {did: summarize_deal_products(rows) for did, rows in product_rows.items()}
        for d in result.get("deals", []):
            d["product"] = product_summary_by_id.get(d["id"], "")
    except Exception as e:
        print(f"  ⚠ не вдалося категоризувати товарообіг: {e}")
        result["deal_categories"] = []
        result["deal_categories_error"] = str(e)

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
