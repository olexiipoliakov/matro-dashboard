# -*- coding: utf-8 -*-
"""
fetch_feed.py — товари без опису за товарним фідом matroluxe.ua.

Чому саме фід, а не сканер сторінок. Перевірити можна трьома способами, і
вони дають різні відповіді:

    monomarket-фід   599 товарів з порожнім <description>   — правда
    сторінка сайту   опису немає, є таблиця характеристик   — правда
    Google-фід       нуль порожніх                          — ні: генератор
                     складає опис із характеристик, щоб товар не відхилили
                     в Merchant Center

Перевірено вручну на картці shkafy-3141: у фіді для Monobank опис порожній,
на сторінці — тільки назва й характеристики, у Google-фіді — 948 символів
склеєних переваг. Тому джерелом правди тут узято monomarket-фід, і числа на
сторінці збігаються з тим, що рахував підрядник, до одиниці.

Google-фід усе одно потрібен, але для іншого: у monomarket немає посилання
на картку товару, а в Google є `g:link`, і `g:id` там дорівнює `code` у
monomarket. Зіставляємо за артикулом — не за назвою: шість різних шаф
називаються однаково, і за назвою вони злилися б в один рядок.

Змінні середовища:
    FEED_URL         monomarket XML (джерело правди про описи)
    GOOGLE_FEED_URL  Google-фід, лише заради посилань. Не заданий — посилання
                     підставляються з останнього скану сайту за назвою, і то
                     лише для назв, які зустрічаються один раз.
"""
import html as html_mod
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
OUT_FILE = BASE_DIR / "feed_data.json"
AUDIT_FILE = BASE_DIR / "audit_data.json"

FEED_URL = os.environ.get(
    "FEED_URL", "https://matroluxe.ua/index.php?route=feed/monomarket/xml")
GOOGLE_FEED_URL = os.environ.get("GOOGLE_FEED_URL", "").strip()
TIMEOUT = int(os.environ.get("FEED_TIMEOUT", "120"))
UA = "Mozilla/5.0 (compatible; MatroDashboardFeed/1.0; +https://matroluxe.ua)"

TAG_RE = re.compile(r"<[^>]+>")


def local(tag):
    """Ім'я тега без простору імен: <g:description> і <description> — те саме."""
    return tag.split("}")[-1].lower()


def fields(el):
    return {local(ch.tag): (ch.text or "") for ch in el}


def plain_text(raw):
    """HTML-опис як чистий текст. &nbsp; перетворюємо на пробіл саме тут:
    інакше <p>&nbsp;</p> виглядає як опис завдовжки шість символів."""
    txt = TAG_RE.sub(" ", raw or "")
    txt = html_mod.unescape(txt).replace("\xa0", " ")
    return re.sub(r"\s+", " ", txt).strip()


# Розміри в назві: 100*200*45, 100х200, 240*240*6. Саме вони перетворюють
# дев'ять моделей на шістсот позицій, тому для групування їх відрізаємо.
SIZE_RE = re.compile(r"\b\d{2,4}\s*[*xхX×]\s*\d{2,4}(\s*[*xхX×]\s*\d{2,4})?\b")


def model_name(title):
    """Назва моделі без розмірів — у тому вигляді, у якому її читати людині."""
    return re.sub(r"\s{2,}", " ", SIZE_RE.sub(" ", str(title or ""))).strip(" -–—,")


def model_key(title):
    return re.sub(r"\s+", " ", model_name(title).lower()).strip()


def norm_title(s):
    s = str(s or "").lower().replace("\xa0", " ")
    s = s.replace("—", "-").replace("–", "-").replace("’", "'")
    s = re.sub(r"[^0-9a-zа-яіїєґ'\- ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def get(session, url, what):
    r = session.get(url, timeout=TIMEOUT, headers={"User-Agent": UA})
    r.raise_for_status()
    print(f"  {what}: {len(r.content) / 1048576:.1f} МБ")
    return r.content


def google_links(xml_bytes):
    """{артикул: адреса картки} з Google-фіда. Опис звідти навмисно не
    береться — він там синтетичний (див. коментар угорі файлу)."""
    root = ET.fromstring(xml_bytes)
    out = {}
    for el in root.iter():
        if local(el.tag) != "entry":
            continue
        f = fields(el)
        code = (f.get("id") or "").strip()
        link = (f.get("link") or "").strip()
        if code and link:
            out[code] = link
    return out


def title_links():
    """Запасний спосіб знайти адресу картки — за назвою з останнього скану
    сайту. Беремо тільки назви, які там зустрічаються РІВНО один раз: інакше
    шість однойменних шаф отримали б одне й те саме посилання, і людина,
    клацнувши, правила б не той товар."""
    try:
        data = json.loads(AUDIT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    seen = {}
    for p in data.get("products") or []:
        key = norm_title(p.get("name"))
        if not key:
            continue
        seen[key] = None if key in seen else p.get("url", "")
    return {k: v for k, v in seen.items() if v}


def parse(xml_bytes, links_by_code, links_by_title):
    """Рядок списку — модель, а не позиція фіда.

    Причина в тому, що показали дані: 599 товарів без опису — це дев'ять
    моделей, решта різниця лише в розмірі, кольорі й начинці. Опис у них
    спільний, пишеться один раз. Список із шестисот майже однакових рядків
    ніхто не читає двічі, а список із дев'яти — це план роботи на тиждень.

    Окремо розрізняємо два випадки, бо це різні виконавці:
        copy  — у якогось варіанта моделі текст уже є, треба його розставити
        write — тексту немає в жодного варіанта, треба писати
    """
    root = ET.fromstring(xml_bytes)
    offers = [el for el in root.iter() if local(el.tag) == "offer"]

    groups = {}
    total = with_text = missing = 0
    for el in offers:
        total += 1
        f = fields(el)
        raw = f.get("description", "")
        has_text = bool(plain_text(raw))
        title = (f.get("title") or "").strip()
        code = (f.get("code") or "").strip()
        key = model_key(title)
        if not key:
            key = "id-" + (f.get("id") or "").strip()

        g = groups.setdefault(key, {
            "key": "feed:" + key, "name": model_name(title) or title,
            "category": (f.get("category") or "—").strip() or "—",
            "empty": 0, "filled": 0, "markup": 0,
            "sample_empty": "", "sample_filled": "",
        })
        if has_text:
            with_text += 1
            g["filled"] += 1
            if not g["sample_filled"]:
                g["sample_filled"] = links_by_code.get(code) or links_by_title.get(norm_title(title), "")
            continue

        missing += 1
        g["empty"] += 1
        if raw.strip():
            g["markup"] += 1
        if not g["sample_empty"]:
            g["sample_empty"] = links_by_code.get(code) or links_by_title.get(norm_title(title), "")

    items, by_cat = [], {}
    for g in groups.values():
        if not g["empty"]:
            continue
        by_cat[g["category"]] = by_cat.get(g["category"], 0) + 1
        items.append({
            "key": g["key"],
            "title": g["name"],
            "category": g["category"],
            "variants": g["empty"],
            "with_text": g["filled"],
            "markup": g["markup"],
            "kind": "copy" if g["filled"] else "write",
            "url": g["sample_empty"],
            "url_filled": g["sample_filled"],
        })

    items.sort(key=lambda i: (i["kind"] != "write", -i["variants"]))
    to_write = [i for i in items if i["kind"] == "write"]
    to_copy = [i for i in items if i["kind"] == "copy"]
    return {
        "generated_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "source": FEED_URL,
        "total": total,
        "with_text": with_text,
        # missing — позиції фіда (те число, яке називав підрядник),
        # missing_cards — моделі, тобто скільки насправді треба текстів.
        "missing": missing,
        "missing_cards": len(items),
        "to_write": len(to_write),
        "to_write_positions": sum(i["variants"] for i in to_write),
        "to_copy": len(to_copy),
        "to_copy_positions": sum(i["variants"] for i in to_copy),
        "linked": sum(1 for i in items if i["url"]),
        "by_category": [{"name": k, "count": v} for k, v in
                        sorted(by_cat.items(), key=lambda kv: -kv[1])],
        "items": items,
    }


if __name__ == "__main__":
    print("=== Feed: описи товарів ===")
    started = time.time()
    session = requests.Session()

    links_by_code = {}
    if GOOGLE_FEED_URL:
        try:
            links_by_code = google_links(get(session, GOOGLE_FEED_URL, "Google-фід"))
            print(f"     посилань на картки: {len(links_by_code)}")
        except Exception as e:
            # Посилання — зручність, а не суть. Без них список лишається
            # списком, тому через Google-фід увесь збір не валимо.
            print(f"  ⚠ Google-фід недоступний ({type(e).__name__}) — "
                  f"посилання підставимо за назвою")
    else:
        print("  GOOGLE_FEED_URL не задано — посилання за назвою зі скану сайту")

    try:
        body = get(session, FEED_URL, "monomarket-фід")
    except Exception as e:
        print(f"✗ не вдалося завантажити фід: {type(e).__name__}: {e}")
        sys.exit(1)

    try:
        data = parse(body, links_by_code, title_links())
    except ET.ParseError as e:
        # Фід іноді віддається з HTML-помилкою замість XML — тоді краще
        # лишити попередній файл, ніж затерти його порожнечею.
        print(f"✗ фід не розібрався як XML: {e}")
        sys.exit(1)

    data["duration_sec"] = round(time.time() - started, 1)
    tmp = OUT_FILE.with_name(OUT_FILE.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, OUT_FILE)

    print(f"✓ позицій у фіді: {data['total']}")
    print(f"  без опису: {data['missing']} позицій — це {data['missing_cards']} моделей")
    print(f"    писати текст : {data['to_write']} моделей / {data['to_write_positions']} позицій")
    print(f"    скопіювати   : {data['to_copy']} моделей / {data['to_copy_positions']} позицій")
    print(f"  з посиланням на картку: {data['linked']} з {data['missing_cards']}")
    for i in data["items"][:10]:
        print(f"    {i['variants']:>4} варіантів | {'писати' if i['kind']=='write' else 'копіювати'} | {i['title'][:56]}")
    for c in data["by_category"][:8]:
        print(f"    {c['name']:<34} {c['count']}")
