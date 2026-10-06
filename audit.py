"""
audit.py — аудит карток товарів на matroluxe.ua.

Що робить:
  1. Бере /ua/sitemap.xml (український основний — міські sitemap'и дублюють
     ті самі товари під префіксом міста, тому їх свідомо не чіпаємо).
     Беруться всі сторінки, включно з головною, блогом і вкладеними
     категоріями: теги видачі потрібні їм так само, як карткам товару.
  2. Викидає блог і службові сторінки, обходить решту й на кожній сторінці
     визначає: це картка товару чи категорія/стаття.
  3. Для КОЖНОЇ сторінки перевіряє теги видачі:
       no_title / no_meta_desc   — тега немає або він порожній
       dup_title / dup_meta_desc — такий самий текст є ще десь на сайті
       title_length / desc_length — задовгий (обріжеться) або закороткий
     Дублі рахуються після обходу всього сайту: поки не побачено всі
     сторінки, про повтор нічого сказати не можна.
  4. Для карток товару додатково перевіряє чотири речі:
       no_description    — вкладки «Опис» немає або вона порожня
       short_description — опис є, але коротший за SHORT_LIMIT символів
       no_photo          — фото немає або стоїть заглушка
       page_error        — сторінка є в sitemap, але віддає 404/500/таймаут
  5. Пише audit_data.json (результат) і audit_progress.json (прогрес,
     який читає /api/audit/status, щоб показувати «150 з 362» на сторінці).

Окремий режим для перевірки самих правил на одній сторінці:
    python3 audit.py --probe https://matroluxe.ua/ua/matras-arlon
Він друкує, який селектор спрацював і що саме знайшлось — зручно, коли
верстка на сайті зміниться і треба зрозуміти, чому аудит став брехати.
"""
import json
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).parent
OUT_FILE = BASE_DIR / "audit_data.json"
PROGRESS_FILE = BASE_DIR / "audit_progress.json"

# Через змінні середовища, щоб можна було прогнати аудит на тестовому
# майданчику, не чіпаючи бойовий сайт.
SITE = os.environ.get("AUDIT_SITE", "https://matroluxe.ua").rstrip("/")
SITEMAP = os.environ.get("AUDIT_SITEMAP", f"{SITE}/ua/sitemap.xml")

# Опис коротший за це вважаємо «відпискою в один рядок».
SHORT_LIMIT = int(os.environ.get("AUDIT_SHORT_LIMIT", "300"))
# Менше цього — вважаємо, що опису фактично немає (лишились крихти розмітки).
EMPTY_LIMIT = 40

WORKERS = int(os.environ.get("AUDIT_WORKERS", "4"))
DELAY = float(os.environ.get("AUDIT_DELAY", "0.35"))   # пауза між запитами в одному потоці
TIMEOUT = 20

UA = "Mozilla/5.0 (compatible; MatroDashboardAudit/1.0; +https://matroluxe.ua)"

ISSUE_TYPES = [
    {"key": "page_error",        "label": "Сторінка не відкривається", "color": "danger"},
    {"key": "no_title",          "label": "Без title",                 "color": "danger"},
    {"key": "no_meta_desc",      "label": "Без meta description",      "color": "danger"},
    {"key": "dup_title",         "label": "Дубль title",               "color": "warning"},
    {"key": "dup_meta_desc",     "label": "Дубль meta description",    "color": "warning"},
    {"key": "title_length",      "label": "Довжина title",             "color": "info"},
    {"key": "desc_length",       "label": "Довжина meta description",  "color": "info"},
    {"key": "no_description",    "label": "Без опису",                 "color": "danger"},
    {"key": "short_description", "label": "Короткий опис",             "color": "warning"},
    {"key": "no_photo",          "label": "Без фото",                  "color": "info"},
]

# Межі довжини title і description. Це не вимога пошукових систем — вони
# нічого не обрізають «по символах», а малюють сніпет по ширині. Але рядок
# довший за TITLE_MAX майже завжди обрізається в видачі, а коротший за
# TITLE_MIN — ознака не написаного, а згенерованого шаблоном заголовка.
TITLE_MIN = int(os.environ.get("AUDIT_TITLE_MIN", "25"))
TITLE_MAX = int(os.environ.get("AUDIT_TITLE_MAX", "65"))
DESC_MIN  = int(os.environ.get("AUDIT_DESC_MIN", "70"))
DESC_MAX  = int(os.environ.get("AUDIT_DESC_MAX", "170"))

# Сторінки, які не є товарами й не мають потрапляти в аудит.
# Сторінки, які не має сенсу перевіряти взагалі: кошик, пошук, вхід.
# Блог і сторінки «Про нас»/«Доставка» тут більше НЕ перелічені: карток товару
# з них не вийде, але title і description їм потрібні так само, як усім іншим,
# а в видачі вони часто стоять на запитах, за якими товарні сторінки не
# ранжуються.
SKIP_PATTERNS = (
    "/index.php", "/search", "/login", "/cart", "/checkout", "/sitemap",
    "/compare", "/wishlist", "?route=",
)

PLACEHOLDER_IMG = ("no_image", "noimage", "no-image", "placeholder", "default.png")


# ── прогрес ──────────────────────────────────────────────────────────────
_lock = threading.Lock()
_done = 0
_total = 0
_last_write = 0.0


def write_progress(phase, force=False):
    """Пише прогрес на диск не частіше разу на 1.5с, щоб не смикати диск
    на кожній з сотень сторінок."""
    global _last_write
    now = time.time()
    if not force and now - _last_write < 1.5:
        return
    _last_write = now
    try:
        PROGRESS_FILE.write_text(json.dumps({
            "phase": phase, "done": _done, "total": _total,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ── збір адрес ───────────────────────────────────────────────────────────
def fetch_sitemap_urls(session):
    r = session.get(SITEMAP, timeout=TIMEOUT)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    # namespace у sitemap'ах стандартний, але буває, що його немає —
    # тому шукаємо по локальному імені тега, а не по повному.
    urls = [el.text.strip() for el in root.iter()
            if el.tag.split("}")[-1] == "loc" and el.text]

    out = []
    for u in urls:
        path = u.replace(SITE, "").rstrip("/")
        # Беремо українську версію разом із самою /ua (головна сторінка: саме
        # вона найчастіше й лишається з тегами «з коробки»). Раніше тут ще
        # відсікались усі вкладені шляхи — через це блог і категорії другого
        # рівня взагалі не перевірялись; для тегів видачі вони потрібні.
        if path != "/ua" and not path.startswith("/ua/"):
            continue
        if is_service_url(u, path):
            continue
        out.append(u)
    return sorted(set(out))


def is_service_url(url, path):
    """Кошик, пошук, вхід — сторінки, яких у видачі бути не повинно.

    Порівнюємо посегментно, а не підрядком: інакше товар зі слагом на кшталт
    «cartoon-matras» вилетів би з аудиту через те, що в ньому є літери «cart».
    """
    segs = [x for x in path.lower().split("/") if x]
    for pat in SKIP_PATTERNS:
        t = pat.strip("/").lower()
        if not t:
            continue
        if "?" in t or "=" in t:
            if t in url.lower():
                return True
        elif any(s == t or s.startswith(t + "-") for s in segs):
            return True
    return False


# ── розбір сторінки ──────────────────────────────────────────────────────
def jsonld(soup):
    """Мікророзмітка сторінки, розкладена по типах: {"Product": {...}, ...}.
    Ми на неї спираємось, бо вона є на кожній картці й не залежить від того,
    як саме зверстані вкладки."""
    out = {}
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.get_text())
        except Exception:
            continue
        for d in (data if isinstance(data, list) else [data]):
            if isinstance(d, dict) and d.get("@type"):
                out.setdefault(d["@type"], d)
    return out


def find_description(soup, ld=None):
    """Повертає (текст, назва_стратегії).

    Основне джерело — вкладка .product_tab_content.tab-description: саме там
    на matroluxe лежить опис (перевірено на живих сторінках). Далі йдуть
    запасні варіанти на випадок зміни теми: мікророзмітка Product.description
    і класична для OpenCart розмітка через id.
    """
    ld = ld if ld is not None else jsonld(soup)
    candidates = []

    el = soup.select_one(".product_tab_content.tab-description")
    if el:
        candidates.append((el, ".product_tab_content.tab-description"))

    el = soup.select_one("#tab-description")
    if el:
        candidates.append((el, "#tab-description"))

    el = soup.select_one('[itemprop="description"]')
    if el:
        candidates.append((el, "itemprop=description"))

    for el in soup.find_all(id=re.compile("description", re.I)):
        candidates.append((el, f"id~{el.get('id')}"))

    for el, how in candidates:
        for junk in el.find_all(["script", "style", "noscript"]):
            junk.decompose()
        text = el.get_text(" ", strip=True)
        if len(text) >= EMPTY_LIMIT:
            return text, how

    # Вкладки немає або вона порожня — питаємо мікророзмітку.
    ld_desc = re.sub(r"\s+", " ", str((ld.get("Product") or {}).get("description") or "")).strip()
    if len(ld_desc) >= EMPTY_LIMIT:
        return ld_desc, "JSON-LD Product.description"

    if candidates:
        return "", candidates[0][1] + " (порожня)"
    return None, None


def has_photo(soup, ld=None):
    ld = ld if ld is not None else jsonld(soup)
    img = (ld.get("Product") or {}).get("image")
    if isinstance(img, list):
        img = img[0] if img else ""
    if img and not any(p in str(img).lower() for p in PLACEHOLDER_IMG):
        return True

    sels = ['[itemprop="image"]', ".thumbnails img", "#product img",
            ".product-image img", ".product-images img", "a.thumbnail img",
            'meta[property="og:image"]']
    for s in sels:
        for el in soup.select(s):
            src = el.get("content") or el.get("src") or el.get("data-src") or ""
            if src and not any(p in src.lower() for p in PLACEHOLDER_IMG):
                return True
    return False


# Плитки товарів у списку. Якщо їх кілька — перед нами підбірка, а не картка.
LISTING_TILE_SELECTOR = (
    ".product-thumb, .product-layout, .product-grid .product, "
    "li.product-item, .product-list .product, .catalog-item"
)
LISTING_MIN_TILES = 3


def product_signals(soup, ld=None):
    """Ознаки сторінки, розкладені по силі — щоб їх було видно в --probe.

    Сильна ознака буває ТІЛЬКИ на картці одного товару: форма додавання в
    кошик, вкладка «Опис», og:type=product. Слабкі (ціна в розмітці, будь-який
    Product у ld+json) є і на сторінках-підбірках, бо їхні плитки теж несуть
    ціну й мікророзмітку — саме через них в аудит потрапили «Дивани»,
    «Комплекти меблів» і «Акція».
    """
    ld = ld if ld is not None else jsonld(soup)
    og = soup.select_one('meta[property="og:type"]')
    ld_product = ld.get("Product") if isinstance(ld.get("Product"), dict) else None
    return {
        "button_cart": bool(soup.select_one("#button-cart, [id^=button-cart]")),
        "product_id_input": bool(soup.select_one('form input[name="product_id"]')),
        "tab_description": bool(soup.select_one(".product_tab_content.tab-description")),
        "og_type_product": bool(og and "product" in (og.get("content") or "").lower()),
        "ld_product_with_offer": bool(ld_product and (ld_product.get("offers") or ld_product.get("sku"))),
        "tiles": len(soup.select(LISTING_TILE_SELECTOR)),
    }


def is_product(soup, ld=None):
    """Картка товару — це сторінка з ознакою САМОГО товару: форма додавання
    в кошик, вкладка «Опис» або og:type=product.

    Мікророзмітку Product за доказ свідомо не беремо: на matroluxe вона стоїть
    і на сторінках-підбірках («Дивани», «Комплекти меблів»), де жодної
    справжньої ознаки немає. Саме через неї розділи потрапляли в аудит як
    товари без опису. Плитки теж не рятують — тема верстає списки своїми
    класами, і лічильник на «Диванах» показав нуль.
    """
    sig = product_signals(soup, ld)
    return bool(sig["button_cart"] or sig["product_id_input"]
                or sig["tab_description"] or sig["og_type_product"])


def product_name(soup, url, ld=None):
    ld = ld if ld is not None else jsonld(soup)
    h1 = soup.select_one("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    nm = (ld.get("Product") or {}).get("name")
    if nm:
        return str(nm).strip()
    og = soup.select_one('meta[property="og:title"]')
    if og and og.get("content"):
        return og["content"].strip()
    return url.rstrip("/").split("/")[-1]


HOME_CRUMBS = ("головна", "главная", "home")


def product_category(soup, ld=None):
    """Категорія — останнє посилання в хлібних крихтах: сам товар там не
    посилання, а звичайний текст (.breadcrumb_last), тому додатково відсікати
    його не треба. Якщо крихт немає — товар не прив'язаний до категорії,
    і тоді чесніше показати прочерк, ніж вигадувати."""
    ld = ld if ld is not None else jsonld(soup)

    crumbs = [a.get_text(strip=True) for a in
              soup.select(".breadcrumbs a, .wrap-span-breadcrumbs a, .breadcrumb a")]
    crumbs = [c for c in crumbs if c and c.lower() not in HOME_CRUMBS]
    if crumbs:
        return crumbs[-1]

    names = [str(i.get("name") or "") for i in
             (ld.get("BreadcrumbList") or {}).get("itemListElement", [])]
    names = [n for n in names if n and n.lower() not in HOME_CRUMBS]
    if len(names) >= 2:          # останній — сам товар, беремо передостанній
        return names[-2]
    return "—"


def page_meta(soup):
    """title і meta description сторінки, вже почищені від переносів.

    Беремо саме <title> і <meta name="description">, а не og:title/og:description:
    у видачу йдуть перші, og — це для соцмереж, і на цьому сайті вони часто
    заповнені тоді, коли звичайні теги порожні. Якщо дивитись на og, аудит
    покаже, що все добре, а в Google сторінка лишиться без заголовка.
    """
    t = soup.find("title")
    title = re.sub(r"\s+", " ", t.get_text(" ", strip=True)).strip() if t else ""
    desc = ""
    for sel in ('meta[name="description"]', 'meta[name="Description"]'):
        m = soup.select_one(sel)
        if m and m.get("content"):
            desc = re.sub(r"\s+", " ", m["content"]).strip()
            break
    return title, desc


def norm_tag(v):
    """Ключ для пошуку дублів. Регістр і зайві пробіли людина не бачить, тож
    «Матрац Арлон» і «матрац  арлон» — це один і той самий заголовок."""
    return re.sub(r"\s+", " ", str(v or "")).strip().lower()


def seo_issues(pages):
    """Знахідки по title і description на всіх зібраних сторінках.

    Дублі рахуються тільки тут, після обходу: поки не побачено весь сайт,
    про повтор нічого сказати не можна. Саме тому ця перевірка не живе
    всередині check_url разом з рештою.
    """
    found = []
    by_title, by_desc = {}, {}
    for pg in pages:
        if pg["title"]:
            by_title.setdefault(norm_tag(pg["title"]), []).append(pg)
        if pg["desc"]:
            by_desc.setdefault(norm_tag(pg["desc"]), []).append(pg)

    def row(pg, issue, detail):
        return {"name": pg["name"], "url": pg["url"], "category": pg["category"],
                "issue": issue, "detail": detail}

    for pg in pages:
        title, desc = pg["title"], pg["desc"]

        if not title:
            found.append(row(pg, "no_title", ""))
        else:
            n = len(title)
            if n > TITLE_MAX:
                found.append(row(pg, "title_length", f"{n} символів — задовгий, обріжеться у видачі"))
            elif n < TITLE_MIN:
                found.append(row(pg, "title_length", f"{n} символів — закороткий"))
            dups = by_title.get(norm_tag(title), [])
            if len(dups) > 1:
                found.append(row(pg, "dup_title",
                                 f"такий самий title ще на {len(dups) - 1} стор."))

        if not desc:
            found.append(row(pg, "no_meta_desc", ""))
        else:
            n = len(desc)
            if n > DESC_MAX:
                found.append(row(pg, "desc_length", f"{n} символів — задовгий, обріжеться у видачі"))
            elif n < DESC_MIN:
                found.append(row(pg, "desc_length", f"{n} символів — закороткий"))
            dups = by_desc.get(norm_tag(desc), [])
            if len(dups) > 1:
                found.append(row(pg, "dup_meta_desc",
                                 f"такий самий опис ще на {len(dups) - 1} стор."))
    return found


def check_url(session, url):
    """Повертає (це_товар, список_знахідок, дані_сторінки) по одній сторінці.

    Третє значення — title/description сторінки. Воно повертається навіть для
    категорій і статей, бо перевірка тегів стосується всього сайту, а не лише
    карток товару: порожній title на категорії коштує дорожче, ніж на одному
    товарі. None означає, що сторінка не відповіла й дивитись там нічого.
    """
    try:
        r = session.get(url, timeout=TIMEOUT)
    except Exception:
        try:
            time.sleep(1.0)
            r = session.get(url, timeout=TIMEOUT)
        except Exception as e:
            return True, [{"name": url.rstrip("/").split("/")[-1], "url": url,
                           "category": "—", "issue": "page_error",
                           "detail": f"немає відповіді: {type(e).__name__}"}], None

    if r.status_code >= 400:
        return True, [{"name": url.rstrip("/").split("/")[-1], "url": url,
                       "category": "—", "issue": "page_error",
                       "detail": f"HTTP {r.status_code}"}], None

    soup = BeautifulSoup(r.text, "html.parser")
    ld = jsonld(soup)
    name = product_name(soup, url, ld)
    cat = product_category(soup, ld)
    title, desc = page_meta(soup)
    meta = {"url": url, "name": name, "category": cat,
            "title": title, "desc": desc, "is_product": is_product(soup, ld)}

    if not meta["is_product"]:
        # Категорія або стаття: вміст картки не перевіряємо, а теги — так.
        return False, [], meta

    found = []

    text, _ = find_description(soup, ld)
    if not text:
        found.append({"name": name, "url": url, "category": cat,
                      "issue": "no_description", "detail": ""})
    elif len(text) < SHORT_LIMIT:
        found.append({"name": name, "url": url, "category": cat,
                      "issue": "short_description",
                      "detail": f"{len(text)} символів"})

    if not has_photo(soup, ld):
        found.append({"name": name, "url": url, "category": cat,
                      "issue": "no_photo", "detail": ""})

    return True, found, meta


# ── прогін ───────────────────────────────────────────────────────────────
def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "uk,ru;q=0.8"})
    return s


def scan():
    global _done, _total
    started = time.time()
    write_progress("Читаю sitemap", force=True)

    session = make_session()
    urls = fetch_sitemap_urls(session)
    _total = len(urls)
    print(f"[audit] до перевірки {_total} сторінок", flush=True)
    write_progress("Перевіряю сторінки", force=True)

    items, errors, products = [], 0, 0
    pages = []          # title/description усіх сторінок, що відповіли
    local = threading.local()

    def worker(url):
        global _done
        if not hasattr(local, "session"):
            local.session = make_session()
        res = check_url(local.session, url)
        time.sleep(DELAY)
        with _lock:
            _done += 1
            write_progress("Перевіряю сторінки")
        return res

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for was_product, res, meta in pool.map(worker, urls):
            if meta:
                pages.append(meta)
                if meta["is_product"]:
                    products += 1
            for row in res:
                if row["issue"] == "page_error":
                    errors += 1
                items.append(row)

    # Дублі видно тільки після обходу всього сайту, тому ця перевірка —
    # окремим проходом по вже зібраних сторінках.
    write_progress("Перевіряю title і description", force=True)
    items.extend(seo_issues(pages))

    # Знаменник здоров'я — усі сторінки, які ми справді відкрили. Раніше тут
    # стояли лише картки товару, бо й перевірялись лише вони; тепер теги
    # перевіряються на всьому сайті, і рахувати відсоток від самих товарів
    # означало б ділити проблеми категорій на кількість матраців.
    checked = len(pages)
    counts = {t["key"]: 0 for t in ISSUE_TYPES}
    for row in items:
        counts[row["issue"]] = counts.get(row["issue"], 0) + 1

    data = {
        "demo": False,
        "generated_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "duration_sec": round(time.time() - started, 1),
        "total_checked": checked,
        "total_products": products,
        "total_errors": errors,
        "issue_types": [dict(t, count=counts.get(t["key"], 0)) for t in ISSUE_TYPES
                        if counts.get(t["key"], 0) > 0],
        "items": items,
    }
    return data


if __name__ == "__main__":
    if "--probe" in sys.argv:
        url = sys.argv[sys.argv.index("--probe") + 1]
        s = make_session()
        r = s.get(url, timeout=TIMEOUT)
        soup = BeautifulSoup(r.text, "html.parser")
        ld = jsonld(soup)
        text, how = find_description(soup, ld)
        sig = product_signals(soup, ld)
        print(f"HTTP           : {r.status_code}")
        print(f"це товар       : {is_product(soup, ld)}")
        print( "  сильні ознаки:")
        for k in ("button_cart", "product_id_input", "tab_description", "og_type_product"):
            print(f"    {k:<22} {'ТАК' if sig[k] else 'ні'}")
        print(f"  плиток товарів у списку: {sig['tiles']} "
              f"(від {LISTING_MIN_TILES} вважаємо сторінкою-підбіркою)")
        print(f"  Product у мікророзмітці з ціною: {'ТАК' if sig['ld_product_with_offer'] else 'ні'}")
        title, mdesc = page_meta(soup)
        print(f"title          : {len(title)} симв. — {title or 'НЕМАЄ'}")
        print(f"description    : {len(mdesc)} симв. — {mdesc[:120] or 'НЕМАЄ'}")
        print(f"назва          : {product_name(soup, url, ld)}")
        print(f"категорія      : {product_category(soup, ld)}")
        print(f"опис знайдено  : {how or 'НІ — жодна стратегія не спрацювала'}")
        print(f"довжина опису  : {len(text) if text else 0} символів "
              f"(поріг короткого — {SHORT_LIMIT})")
        print(f"фото           : {'є' if has_photo(soup, ld) else 'НЕМАЄ'}")
        if text:
            print(f"початок опису  : {text[:160]}…")
        sys.exit(0)

    try:
        data = scan()
        OUT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        write_progress("Готово", force=True)
        print(f"[audit] {data['total_checked']} сторінок, "
              f"{len(data['items'])} знахідок, {data['total_errors']} помилок, "
              f"{data['duration_sec']}с", flush=True)
    except Exception as e:
        write_progress(f"Помилка: {e}", force=True)
        print(f"[audit] критична помилка: {e}", flush=True)
        raise
