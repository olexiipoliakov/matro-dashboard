#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Выгрузка из Meta Ads API в data.json для дашборда.

Особенность: у КАЖДОЙ кампании считается ЕЁ СОБСТВЕННЫЙ результат —
тот, на который кампания оптимизирована (optimization_goal). Так цифра
"обращений" совпадает с колонкой "Результат" в Ads Manager:
  - кампании на переписки  -> начатые переписки в директе
  - кампании на покупки    -> покупки на сайте (пиксель)
  - кампании на лиды        -> лиды / лид-формы
  - кампании на трафик      -> клики по ссылке

Поток:  fetch_meta.py --> data.json --> index.html

Установка:  pip install requests
Запуск:     python fetch_meta.py

Доступы (переменные окружения или впишите ниже):
    META_ACCESS_TOKEN — токен с правом ads_read
    META_ACCOUNT_ID   — act_XXXXXXXXXX из Ads Manager
"""

import os, json, time, datetime as dt
from pathlib import Path
import requests

ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN", "PASTE_YOUR_TOKEN_HERE")
ACCOUNT_ID   = os.environ.get("META_ACCOUNT_ID",   "act_XXXXXXXXXX")
API_VERSION  = "v21.0"
DATE_PRESET  = "last_90d"    # тянем широкий период; фильтр по датам — в дашборде
CURRENCY     = "грн"

# ── Маппинг: цель оптимизации кампании -> какой action_type считать "результатом".
# Meta отдаёт optimization_goal у группы объявлений; для надёжности мы
# определяем результат и по нему, и по фактическим действиям.
# Ключ — optimization_goal (как шлёт Meta), значение — список action_type,
# любой из которых считается результатом этой кампании.
GOAL_TO_ACTION = {
    "CONVERSATIONS":           ["onsite_conversion.messaging_conversation_started_7d"],
    "LEAD_GENERATION":         ["lead", "onsite_conversion.lead_grouped"],
    "QUALITY_LEAD":            ["lead", "onsite_conversion.lead_grouped",
                                "offsite_conversion.fb_pixel_lead"],
    "OFFSITE_CONVERSIONS":     ["offsite_conversion.fb_pixel_purchase",
                                "offsite_conversion.fb_pixel_lead",
                                "purchase"],
    "PURCHASE":                ["offsite_conversion.fb_pixel_purchase", "purchase"],
    "LINK_CLICKS":             ["link_click"],
    "LANDING_PAGE_VIEWS":      ["landing_page_view"],
    "REACH":                   ["link_click"],
    "IMPRESSIONS":             ["link_click"],
    "THRUPLAY":                ["video_view"],
}
# Если цель кампании не нашлась в маппинге — пробуем этот универсальный
# приоритет (берём первый тип, который реально присутствует в действиях).
FALLBACK_PRIORITY = [
    "onsite_conversion.messaging_conversation_started_7d",
    "offsite_conversion.fb_pixel_purchase",
    "offsite_conversion.fb_pixel_lead",
    "lead",
    "onsite_conversion.lead_grouped",
    "purchase",
    "link_click",
]

# Человеческие подписи результата для дашборда
GOAL_LABEL = {
    "CONVERSATIONS":       "Переписки",
    "LEAD_GENERATION":     "Лиды",
    "QUALITY_LEAD":        "Лиды",
    "OFFSITE_CONVERSIONS": "Покупки",
    "PURCHASE":            "Покупки",
    "LINK_CLICKS":         "Клики",
    "LANDING_PAGE_VIEWS":  "Просмотры",
    "REACH":               "Клики",
    "IMPRESSIONS":         "Клики",
    "THRUPLAY":            "Просмотры видео",
}
ACTION_LABEL = {
    "onsite_conversion.messaging_conversation_started_7d": "Переписки",
    "offsite_conversion.fb_pixel_purchase": "Покупки",
    "purchase": "Покупки",
    "offsite_conversion.fb_pixel_lead": "Лиды",
    "lead": "Лиды",
    "onsite_conversion.lead_grouped": "Лиды",
    "link_click": "Клики",
    "landing_page_view": "Просмотры",
    "video_view": "Просмотры видео",
}

OUT = Path(__file__).parent / "data.json"
BASE = f"https://graph.facebook.com/{API_VERSION}"


def _get(url, params, tries=3):
    for i in range(tries):
        r = requests.get(url, params=params, timeout=60)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 503) or "rate" in r.text.lower():
            time.sleep(5 * (i + 1)); continue
        raise RuntimeError(f"Meta API {r.status_code}: {r.text[:300]}")
    raise RuntimeError("Meta API: rate limit, попыток не осталось")


def paged(url, params):
    out = []
    while True:
        d = _get(url, params)
        out += d.get("data", [])
        nxt = d.get("paging", {}).get("next")
        if not nxt:
            break
        url, params = nxt, {}
    return out


def fetch_campaign_goals():
    """
    Возвращает {campaign_id: {"name":..., "goal":..., "actions":[...]}}.
    Цель берём с уровня adset (optimization_goal), агрегируем на кампанию:
    берём наиболее частую цель среди групп объявлений кампании.
    """
    # кампании: id + название
    camps = paged(f"{BASE}/{ACCOUNT_ID}/campaigns",
                  {"access_token": ACCESS_TOKEN, "fields": "id,name", "limit": 300})
    cmap = {c["id"]: {"name": c.get("name", "—"), "goals": {}} for c in camps}

    # группы объявлений: optimization_goal + campaign_id
    adsets = paged(f"{BASE}/{ACCOUNT_ID}/adsets",
                   {"access_token": ACCESS_TOKEN,
                    "fields": "campaign_id,optimization_goal", "limit": 500})
    for a in adsets:
        cid = a.get("campaign_id")
        g = a.get("optimization_goal", "")
        if cid in cmap and g:
            cmap[cid]["goals"][g] = cmap[cid]["goals"].get(g, 0) + 1

    # выбираем доминирующую цель кампании
    result = {}
    for cid, info in cmap.items():
        goal = max(info["goals"], key=info["goals"].get) if info["goals"] else ""
        result[cid] = {
            "name": info["name"],
            "goal": goal,
            "actions": GOAL_TO_ACTION.get(goal, []),
        }
    return result


def count_result(row, wanted, seen):
    """Считает результат строки по нужным типам действий (wanted)."""
    by_type = {}
    for a in row.get("actions", []) or []:
        t = a.get("action_type", "")
        seen.add(t)
        by_type[t] = by_type.get(t, 0) + float(a.get("value", 0))
    # сумма по нужным типам
    if wanted:
        return sum(by_type.get(t, 0) for t in wanted), by_type
    return 0.0, by_type


def pick_fallback(by_type):
    """Если цель неизвестна — берём первый осмысленный тип из приоритета."""
    for t in FALLBACK_PRIORITY:
        if by_type.get(t, 0) > 0:
            return t, by_type[t]
    return "", 0.0


def insights(level, fields, extra=None):
    params = {"access_token": ACCESS_TOKEN, "level": level,
              "fields": fields, "date_preset": DATE_PRESET, "limit": 300}
    if extra:
        params.update(extra)
    return paged(f"{BASE}/{ACCOUNT_ID}/insights", params)


def fetch_thumbnails(ad_ids):
    """
    Возвращает {ad_id: image_url}.
    Пробуем несколько способов получить картинку — от простого к сложному.
    """
    thumbs = {}
    if not ad_ids:
        return thumbs

    CHUNK = 30
    for i in range(0, len(ad_ids), CHUNK):
        chunk = ad_ids[i:i+CHUNK]

        # Способ 1: поле picture и creative напрямую у объявления
        params = {
            "access_token": ACCESS_TOKEN,
            "ids": ",".join(chunk),
            "fields": "creative.fields(thumbnail_url,image_url,picture,effective_object_story_id)",
        }
        try:
            data = _get(f"{BASE}/", params)
        except Exception as e:
            print("  (превью способ 1: ошибка —", str(e)[:80], ")")
            data = {}

        for ad_id, obj in data.items():
            cr = obj.get("creative", {}) or {}
            url = (cr.get("picture") or cr.get("image_url") or
                   cr.get("thumbnail_url") or "")
            thumbs[ad_id] = url

        # Способ 2: для тех кто не получил — запросить adcreatives отдельно
        missing = [aid for aid in chunk if not thumbs.get(aid)]
        if missing:
            params2 = {
                "access_token": ACCESS_TOKEN,
                "ids": ",".join(missing),
                "fields": "adcreatives{thumbnail_url,picture,image_url}",
            }
            try:
                data2 = _get(f"{BASE}/", params2)
                for ad_id, obj in data2.items():
                    acs = obj.get("adcreatives", {}).get("data", []) or []
                    for ac in acs:
                        url = (ac.get("picture") or ac.get("image_url") or
                               ac.get("thumbnail_url") or "")
                        if url:
                            thumbs[ad_id] = url
                            break
            except Exception as e:
                print("  (превью способ 2: ошибка —", str(e)[:80], ")")

    return thumbs


def _best_image(cr):
    """Выбирает самую крупную доступную картинку из креатива."""
    # 1) полноразмерная картинка
    if cr.get("image_url"):
        return cr["image_url"]
    # 2) картинка из object_story_spec (link_data / video_data)
    spec = cr.get("object_story_spec", {}) or {}
    for k in ("link_data", "video_data"):
        d = spec.get(k, {}) or {}
        if d.get("picture"):
            return d["picture"]
        if d.get("image_url"):
            return d["image_url"]
    # 3) миниатюра — апскейлим параметры размера в URL Meta
    t = cr.get("thumbnail_url", "")
    if t:
        # Meta-миниатюры часто содержат p64x64 / s64x64 — поднимаем
        for sm, big in (("p64x64", "p600x600"), ("s64x64", "s600x600"),
                        ("p128x128", "p600x600")):
            t = t.replace(sm, big)
    return t


def download_images(banners):
    """
    Скачивает картинки баннеров локально в папку images/.
    Заменяет внешние ссылки на локальные пути — тогда браузер
    грузит их без блокировок CDN.
    Файлы называются по хэшу URL, чтобы не скачивать повторно.
    """
    import hashlib
    img_dir = OUT.parent / "images"
    img_dir.mkdir(exist_ok=True)

    for b in banners:
        url = b.get("image", "")
        if not url or not url.startswith("http"):
            continue
        # имя файла = хэш URL (не зависит от временных параметров)
        # берём часть URL до "?" чтобы определить расширение
        base_url = url.split("?")[0]
        ext = base_url.rsplit(".", 1)[-1].lower()
        if ext not in ("jpg", "jpeg", "png", "webp", "gif"):
            ext = "jpg"
        fname = hashlib.md5(base_url.encode()).hexdigest()[:16] + "." + ext
        fpath = img_dir / fname
        if not fpath.exists():
            try:
                r = requests.get(url, timeout=15,
                                 headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    fpath.write_bytes(r.content)
                else:
                    b["image"] = ""
                    continue
            except Exception:
                b["image"] = ""
                continue
        b["image"] = f"images/{fname}"


def build():
    seen = set()
    print("→ Тяну цели кампаний (optimization_goal)…")
    goals = fetch_campaign_goals()

    # карта имя->цель (на случай если в insights нет campaign_id)
    name_goal = {info["name"]: info for info in goals.values()}

    # 1) по дням × кампания
    print("→ Тяну статистику по дням…")
    daily_raw = insights("campaign",
                          "campaign_id,campaign_name,spend,actions,date_start",
                          {"time_increment": 1})
    daily = []
    goal_used = {}
    for r in daily_raw:
        cid = r.get("campaign_id")
        cname = r.get("campaign_name", "—")
        info = goals.get(cid) or name_goal.get(cname) or {"goal": "", "actions": []}
        spend = float(r.get("spend", 0) or 0)
        res, by_type = count_result(r, info["actions"], seen)
        used_goal = info["goal"]
        if res == 0 and not info["actions"]:
            # цель неизвестна — fallback
            t, res = pick_fallback(by_type)
            used_goal = used_goal or "(авто)"
        goal_used[cname] = used_goal
        daily.append({
            "date": r.get("date_start"),
            "campaign": cname,
            "goal": GOAL_LABEL.get(used_goal, "Обращения"),
            "spend": round(spend, 2),
            "leads": int(round(res)),
            "cost_per_lead": round(spend / res, 2) if res else 0,
        })

    # 2) баннеры (объявления) — АГРЕГАТ за период с точными результатами.
    # Без разбивки по дням: Meta корректно отдаёт actions только на агрегате,
    # подневная разбивка по объявлениям рассыпает конверсии и даёт нули.
    print("→ Тяну статистику по объявлениям (за период)…")
    ads_raw = insights("ad",
                        "ad_id,ad_name,campaign_id,campaign_name,spend,actions,ctr")

    # 2b) картинки креативов
    print("→ Тяну превью баннеров…")
    ad_ids = list({r.get("ad_id") for r in ads_raw if r.get("ad_id")})

    # диагностика: смотрим что вообще возвращает API для первого объявления
    if ad_ids:
        try:
            diag = _get(f"{BASE}/", {
                "access_token": ACCESS_TOKEN,
                "ids": ad_ids[0],
                "fields": "name,creative.fields(thumbnail_url,image_url,picture)"
            })
            print("  [диаг] поля первого объявления:", list(diag.get(ad_ids[0], {}).keys()))
            cr = diag.get(ad_ids[0], {}).get("creative", {}) or {}
            print("  [диаг] поля creative:", list(cr.keys()))
            print("  [диаг] picture:", bool(cr.get("picture")),
                  "| image_url:", bool(cr.get("image_url")),
                  "| thumbnail_url:", bool(cr.get("thumbnail_url")))
        except Exception as e:
            print("  [диаг] ошибка:", str(e)[:120])

    thumbs = fetch_thumbnails(ad_ids)

    banners = []
    for r in ads_raw:
        cid = r.get("campaign_id")
        cname = r.get("campaign_name", "—")
        info = goals.get(cid) or name_goal.get(cname) or {"goal": "", "actions": []}
        spend = float(r.get("spend", 0) or 0)
        res, by_type = count_result(r, info["actions"], seen)
        if res == 0 and not info["actions"]:
            _, res = pick_fallback(by_type)
        banners.append({
            "banner": r.get("ad_name", "—"),
            "campaign": cname,
            "goal": GOAL_LABEL.get(info["goal"], "Обращения"),
            "spend": round(spend, 2),
            "leads": int(round(res)),
            "cost_per_lead": round(spend / res, 2) if res else 0,
            "ctr": round(float(r.get("ctr", 0) or 0), 2),
            "image": thumbs.get(r.get("ad_id"), ""),
        })

    print("→ Скачиваю картинки баннеров локально…")
    download_images(banners)

    dates = sorted({d["date"] for d in daily if d["date"]})
    period = f"{dates[0]} — {dates[-1]}" if dates else DATE_PRESET

    out = {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": "Meta Marketing API",
        "period": period,
        "currency": CURRENCY,
        "daily": daily,
        "banners": banners,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n✓ data.json готов: строк день×кампания {len(daily)}, баннеров {len(banners)}")
    with_img = sum(1 for b in banners if b.get("image"))
    print(f"  Баннеров с картинкой: {with_img} из {len(banners)}")
    sample = next((b["image"] for b in banners if b.get("image")), "")
    if sample:
        print(f"  Пример ссылки на превью: {sample[:90]}…")
    if with_img == 0:
        print("  ⚠ Картинки не пришли. Возможно, токену не хватает прав на креативы.")
    print("\n  Какая цель определена у каждой кампании:")
    for name, g in sorted(goal_used.items()):
        print(f"   - {name}  →  {GOAL_LABEL.get(g, g or '(авто)')}")


if __name__ == "__main__":
    if "PASTE_YOUR_TOKEN" in ACCESS_TOKEN or "XXXX" in ACCOUNT_ID:
        print("⚠  Укажите META_ACCESS_TOKEN и META_ACCOUNT_ID.")
    else:
        build()
