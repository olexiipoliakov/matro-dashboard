cd ~/Desktop/matro-dashboard
git checkout -- .
git pull --rebase
pbpaste > kpi-trend.js
python3 - <<'PY'
import pathlib
p = pathlib.Path("index.html"); s = p.read_text(encoding="utf-8")
tag = '<script src="kpi-trend.js?v=1"></script>'
if tag in s:
    print("скрипт уже подключён")
else:
    assert s.count("</body>") == 1, "не нашёл </body>"
    p.write_text(s.replace("</body>", tag + "\n</body>", 1), encoding="utf-8")
    print("подключено")
PY
head -c 60 kpi-trend.js; echo; wc -c kpi-trend.js