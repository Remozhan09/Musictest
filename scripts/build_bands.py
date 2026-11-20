"""Собирает data/*.txt в site/bands.json.

Строка: Группа | Трек | страна | жанр | *   (трек можно оставить пустым, * = андеграунд)
Повторы групп убираются (оставляется первая строка с непустым треком).
Результат: [["Группа","Трек","страна","жанр",андеграунд(0/1)], ...]
"""
import glob, json, os, re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GENRES = {"emo", "scr", "mc", "phc", "dc", "math", "pp", "alt"}


def key(name):
    return re.sub(r"[\W_]+", "", name.lower().replace("ё", "е"))


order, bands = [], {}
for path in sorted(glob.glob(os.path.join(ROOT, "data", "*.txt"))):
    for n, line in enumerate(open(path, encoding="utf-8"), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = [x.strip() for x in line.split("|")]
        if len(p) < 4 or p[3] not in GENRES:
            raise SystemExit(f"{path}:{n}: плохая строка: {line}")
        name, track, cc, genre = p[:4]
        odd = 1 if len(p) > 4 and p[4] == "*" else 0
        k = key(name)
        if k in bands:
            if track and not bands[k][1]:
                bands[k][1] = track
            continue
        order.append(k)
        bands[k] = [name, track, cc, genre, odd]

out = [bands[k] for k in order]
json.dump(out, open(os.path.join(ROOT, "site", "bands.json"), "w", encoding="utf-8"),
          ensure_ascii=False, separators=(",", ":"))
ru = sum(1 for b in out if b[2] in ("ru", "ua", "by", "kz", "md"))
print(f"Групп: {len(out)} (СНГ: {ru}, зарубежных: {len(out) - ru}), с треком: {sum(1 for b in out if b[1])}")
