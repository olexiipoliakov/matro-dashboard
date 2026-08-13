# AGENTS.md

## О проекте

Matro — производитель матрасов и мебели (Украина). Этот репозиторий — статический дашборд
(GitHub Pages) для интернет-подразделения, которое ведёт маркетинг, маркетплейсы (Rozetka,
Simpler, Epicentr), аналитику и разработку.

Дашборд отслеживает два источника данных:
- **SMM / Meta Ads** — расходы, лиды, эффективность кампаний и баннеров для бренда Plume
- **SEO** — Google Search Console для matroluxe.ua: клики, показы, позиции, динамика по разделам сайта

Живой сайт: https://olexiipoliakov.github.io/matro-dashboard/home.html
`home.html` — навигационный хаб, ссылается на `index.html` (SMM) и `seo.html` (SEO).

## Структура репозитория (коротко)

- `index.html`, `seo.html`, `home.html` — статические страницы дашборда, каждая читает свой JSON через `fetch()`
- `data.json` / `seo_data.json` — данные, генерируются автоматически, руками не редактируются
- `fetch_meta.py` / `fetch_gsc.py` — Python-скрипты выгрузки из Meta Ads API / Google Search Console API
- `.github/workflows/update.yml` — ежедневный (06:00 UTC) прогон обоих скриптов + коммит результата
- `docs/codemap/` — техническая карта кода (см. ниже) — для полной детализации связей между файлами открой `docs/codemap/codemap.html`

## Важные особенности и договорённости

- Все страницы используют единую дизайн-систему: `index.html`/`seo.html`/`home.html` — кремовая палитра (`#f5f1ea`/`#fffdf9`), Segoe UI; акценты по модулю: терракота `#c8553d` = SMM, зелёно-синий `#3ecf8e`/`#5b8dee` = SEO. Новые страницы дашборда должны следовать этой же логике.
- `fetch_gsc.py`: сайт matroluxe.ua зарегистрирован в GSC как URL-prefix property — `site_url` всегда должен быть `https://matroluxe.ua/`, а не `sc-domain:matroluxe.ua` (иначе 403).
- `classify_category()` в `fetch_gsc.py` — эвристика по ключевым словам в URL (сайт плоский, без `/catalog/`), может ошибаться на отдельных товарах — не считать это багом при первом взгляде.
- `data.json` содержит только аккаунт Plume; поддержка второго аккаунта ("Matro") в `index.html` (вкладка, дергающая `data2.json`) — неиспользуемый остаток, этот файл никто не генерирует.
- Данные никогда не редактируются вручную — только через `.github/workflows/update.yml` (fetch-скрипты → commit → push).
- Локальная папка для разработки: `C:\Users\User\OneDrive\Desktop\matro-dashboard\`; стандартный пуш — `git add` → `git commit` → `git push` (при отказе — `git pull --rebase` → `git push`).

## Code map (docs/codemap/)

- `docs/codemap/codemap.json`, `codemap.html`, and `codemap.lock` describe the repo's modules, data flows, and end-to-end scenarios. They must always reflect the current state of the code — never left stale.
- Regenerate only when the change is **structural**: a file is added, removed, or renamed; a new (or removed) call/read/write/import/publish relationship appears between modules; a new external dependency shows up; a new end-to-end flow is added. Internal logic changes within an existing file/function (bugfixes, new fields on an existing data shape, UI tweaks, new columns in an existing table, refined retry logic, etc.) do NOT require a codemap regeneration on their own — only touch the map when the *shape* of the system (nodes/edges/flows) actually changed.
- When in doubt whether something is structural, err on the side of *not* regenerating — a stale prose description inside an existing node's `role` is a much smaller cost than interrupting every small change with a map rebuild.
- Compare `docs/codemap/codemap.lock` against the current repo to decide if anything structural changed:
  - If any tracked module's fingerprint no longer matches its current files, that's a signal something changed inside it — but only regenerate if that change was structural per the rule above, not for every fingerprint drift.
  - If a new top-level file/module exists that isn't in the lock, treat it as new — regenerate.
- Regenerate all three files together (`codemap.json`, `codemap.html`, `codemap.lock`) — never edit just one by hand. `codemap.html` must embed the exact same nodes/edges/flows as `codemap.json`.
- Every node, edge, and flow must be backed by real evidence from the source (file path + line/anchor). If a relationship can't be verified in the code, mark it `unknown` — do not guess.
- Do not modify product code while regenerating the code map. This is a read-and-document task only.
- After regenerating, verify: JSON parses, every node path exists (or is explicitly marked as not-yet-present with a reason, like a data file a workflow hasn't produced yet), every edge/flow step references a real node id, and `codemap.lock` matches the current commit and module fingerprints.
