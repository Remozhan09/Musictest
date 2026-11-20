"""Расширяет список групп с Last.fm и находит лучший трек каждой группы.

1. Собирает кандидатов со страниц тегов last.fm/tag/<тег>/artists (русские теги — в первую очередь).
2. Открывает страницу каждой группы: берёт топ-трек (№1 по прослушиваниям), теги, число слушателей.
   Группы, у которых основные теги — рэп/поп/электроника, пропускаются.
3. Добавляет самых популярных, пока всего групп не станет target_total.

Пишет:
  data/zz_lastfm.txt          — новые группы (тот же формат, что и остальные data/*.txt)
  data/lastfm_tracks.json     — топ-треки для групп из ручного списка, у которых трек не указан
  data/lastfm_report.txt      — отчёт
Настройки — data/lastfm.json. Повторный запуск с теми же настройками ничего не делает.
"""
import html, json, os, re, sys, time, threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus, unquote_plus

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CFG = os.path.join(DATA, "lastfm.json")
DONE = os.path.join(DATA, "lastfm_done.json")
OUT = os.path.join(DATA, "zz_lastfm.txt")
TRACKS = os.path.join(DATA, "lastfm_tracks.json")
REPORT = os.path.join(DATA, "lastfm_report.txt")
BASE = "https://www.last.fm"

# тег -> жанр; порядок = приоритет при сборе кандидатов
RU_TAGS = [("russian emo", "emo"), ("russian post-hardcore", "phc"), ("russian metalcore", "mc"),
           ("russian screamo", "scr"), ("russian deathcore", "dc"), ("russian mathcore", "math"),
           ("russian pop-punk", "pp"), ("russian alternative metal", "alt"), ("russian nu metal", "alt"),
           ("emo russian", "emo")]
TAGS = [("screamo", "scr"), ("skramz", "scr"), ("emo", "emo"), ("midwest emo", "emo"), ("emocore", "emo"),
        ("post-hardcore", "phc"), ("metalcore", "mc"), ("melodic metalcore", "mc"), ("deathcore", "dc"),
        ("mathcore", "math"), ("easycore", "pp"), ("pop punk", "pp"), ("nu metalcore", "mc")]
GENRE_TAGS = {"emo": "emo", "screamo": "scr", "skramz": "scr", "emocore": "emo", "midwest emo": "emo",
              "post-hardcore": "phc", "metalcore": "mc", "melodic metalcore": "mc", "deathcore": "dc",
              "mathcore": "math", "pop punk": "pp", "pop-punk": "pp", "easycore": "pp",
              "alternative metal": "alt", "nu metal": "alt", "hardcore": "mc", "melodic hardcore": "phc"}
BAD_TAGS = {"hip-hop", "hip hop", "rap", "emo rap", "trap", "cloud rap", "pop", "electronic", "rnb", "r&b",
            "dance", "edm", "hyperpop", "russian rap", "russian hip-hop", "phonk", "witch house", "k-pop"}
COUNTRY_TAGS = [("russian", "ru"), ("russia", "ru"), ("ukrainian", "ua"), ("ukraine", "ua"),
                ("belarusian", "by"), ("belarus", "by"), ("kazakh", "kz"), ("american", "us"), ("usa", "us"),
                ("british", "uk"), ("uk", "uk"), ("english", "uk"), ("welsh", "uk"), ("scottish", "uk"),
                ("canadian", "ca"), ("australian", "au"), ("german", "de"), ("japanese", "jp"), ("swedish", "se"),
                ("french", "fr"), ("finnish", "fi"), ("brazilian", "br"), ("mexican", "mx"), ("polish", "pl"),
                ("italian", "it"), ("dutch", "nl"), ("danish", "dk"), ("norwegian", "no")]

if not os.path.exists(CFG):
    print("Нет data/lastfm.json — пропускаю"); sys.exit(0)
cfg = json.load(open(CFG, encoding="utf-8"))
done = json.load(open(DONE, encoding="utf-8")) if os.path.exists(DONE) else []
if cfg in done:
    print("Это расширение уже сделано — пропускаю"); sys.exit(0)
TARGET = cfg.get("target_total", 2000)
PAGES = cfg.get("pages_per_tag", 15)
RU_MIN = cfg.get("ru_min_listeners", 300)
MIN = cfg.get("min_listeners", 3000)
CAPS = cfg.get("caps", {"pp": 200, "alt": 60})

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/124.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"})
lock = threading.Lock()
log = []


def norm(s):
    return re.sub(r"[\W_]+", "", (s or "").lower().replace("ё", "е"))


def get(url):
    for attempt in range(4):
        try:
            r = S.get(url, timeout=25)
            if r.status_code == 200:
                return r.text
            if r.status_code == 404:
                return None
            time.sleep(3 * (attempt + 1))
        except Exception:
            time.sleep(3 * (attempt + 1))
    return None


def num(s):
    s = re.sub(r"[^\d]", "", s or "")
    return int(s) if s else 0


def parse_tag_page(page):
    """[(name, href, listeners)] со страницы /tag/<tag>/artists"""
    out = []
    for m in re.finditer(r'big-artist-list-title[^>]*>\s*<a[^>]*href="(/music/[^"/?#]+)"[^>]*>([^<]+)</a>'
                         r'(.{0,600}?)big-artist-list-listeners[^>]*>\s*([\d,.\s]+)', page, re.S):
        out.append((html.unescape(m.group(2)).strip(), m.group(1), num(m.group(4))))
    if not out:  # запасной разбор: просто ссылки на артистов по порядку
        seen = set()
        for m in re.finditer(r'<a[^>]*href="(/music/[^"/?#+]+(?:\+[^"/?#]+)*)"[^>]*>([^<]{1,80})</a>', page):
            href = m.group(1)
            if href not in seen and "/_/" not in href:
                seen.add(href)
                out.append((html.unescape(m.group(2)).strip(), href, 0))
    return out


def parse_artist(page):
    tags = [html.unescape(t).lower() for t in re.findall(r'<li class="tag">\s*<a[^>]*>([^<]+)</a>', page)]
    m = re.search(r'Listeners</h4>.{0,400}?title="([\d,]+)"', page, re.S)
    listeners = num(m.group(1)) if m else 0
    m = re.search(r'chartlist-name[^>]*>\s*<a[^>]*title="([^"]+)"', page)
    if not m:
        m = re.search(r'chartlist-name[^>]*>\s*<a[^>]*>([^<]+)</a>', page)
    track = html.unescape(m.group(1)).strip() if m else ""
    return tags, listeners, track


def country(name, tags, default=""):
    if re.search(r"[а-яё]", name, re.I):
        default = default or "ru"
    for t in tags[:10]:
        for word, cc in COUNTRY_TAGS:
            if re.search(rf"\b{re.escape(word)}\b", t):
                return cc
    return default


def artist_info(href):
    page = get(BASE + href)
    if not page:
        return None
    return parse_artist(page)


# ---------- существующие группы ----------
existing = []
for path in sorted(os.listdir(DATA)):
    if path.endswith(".txt") and path != os.path.basename(OUT):
        for line in open(os.path.join(DATA, path), encoding="utf-8"):
            p = [x.strip() for x in line.split("|")]
            if len(p) >= 4 and not line.startswith("#"):
                existing.append(p)
have = {norm(p[0]) for p in existing}
need_track = {}
for p in existing:
    if not p[1] and norm(p[0]) not in need_track:
        need_track[norm(p[0])] = p[0]
print(f"В ручном списке: {len(have)} групп, без трека: {len(need_track)}")

# ---------- 1. кандидаты со страниц тегов ----------
cands = {}  # norm -> dict
first_page_dump = None
for group, tags in (("ru", RU_TAGS), ("intl", TAGS)):
    for tag, genre in tags:
        got = 0
        for pg in range(1, PAGES + 1):
            page = get(f"{BASE}/tag/{quote_plus(tag)}/artists?page={pg}")
            if not page:
                break
            items = parse_tag_page(page)
            if first_page_dump is None:
                first_page_dump = page
            if not items:
                break
            for name, href, lis in items:
                k = norm(name)
                if not k:
                    continue
                c = cands.get(k)
                if c is None:
                    cands[k] = {"name": name, "href": href, "lis": lis, "genre": genre, "ru": group == "ru", "n": len(cands)}
                else:
                    c["lis"] = max(c["lis"], lis)
                    c["ru"] = c["ru"] or group == "ru"
                got += 1
            time.sleep(0.4)
        log.append(f"тег «{tag}»: {got}")
        print(f"тег «{tag}»: {got}")

if not cands:
    open(os.path.join(DATA, "lastfm_debug.html"), "w", encoding="utf-8").write((first_page_dump or "нет ответа")[:200000])
    raise SystemExit("Last.fm: не удалось разобрать страницы тегов (см. data/lastfm_debug.html)")

# ---------- 2. топ-треки для групп без трека ----------
tracks = {}


def fill_track(item):
    k, name = item
    c = cands.get(k)
    href = c["href"] if c else "/music/" + quote_plus(name)
    info = artist_info(href)
    if info and info[2]:
        with lock:
            tracks[name] = info[2]


with ThreadPoolExecutor(4) as pool:
    list(pool.map(fill_track, need_track.items()))
log.append(f"топ-треков для ручного списка: {len(tracks)} из {len(need_track)}")
print(log[-1])

# ---------- 3. новые группы ----------
new = [c for k, c in cands.items() if k not in have]
# если число слушателей со страницы тега не разобралось (0) — берём в порядке тега
ru = sorted([c for c in new if c["ru"] and (c["lis"] >= RU_MIN or not c["lis"])], key=lambda c: (-c["lis"], c["n"]))
intl = sorted([c for c in new if not c["ru"] and (c["lis"] >= MIN or not c["lis"])], key=lambda c: (-c["lis"], c["n"]))
queue = ru + intl
want = TARGET - len(have)
print(f"Кандидатов: русских {len(ru)}, зарубежных {len(intl)}, нужно добавить {want}")
added, rejected, per_genre = [], 0, {}


def check(c):
    info = artist_info(c["href"])
    time.sleep(0.3)
    return c, info


i = 0
with ThreadPoolExecutor(4) as pool:
    while len(added) < want and i < len(queue):
        batch = queue[i:i + 40]
        i += 40
        for c, info in pool.map(check, batch):
            if len(added) >= want:
                break
            if not info:
                rejected += 1
                continue
            tags, lis, track = info
            top = tags[:4]
            if any(t in BAD_TAGS for t in top[:2]) and not any(t in GENRE_TAGS for t in top):
                rejected += 1
                log.append(f"пропуск (не тот жанр: {', '.join(top)}): {c['name']}")
                continue
            genre = next((GENRE_TAGS[t] for t in tags[:6] if t in GENRE_TAGS), c["genre"])
            cap = CAPS.get(genre)
            if cap is not None and per_genre.get(genre, 0) >= cap:
                continue
            cc = country(c["name"], tags, "ru" if c["ru"] else "")
            if c["ru"] and cc not in ("ru", "ua", "by", "kz", "md"):
                cc = "ru"
            per_genre[genre] = per_genre.get(genre, 0) + 1
            lis = lis or c["lis"]
            odd = "*" if lis < (20000 if c["ru"] else 100000) else ""
            added.append(f"{c['name'].replace('|', '/')} | {track.replace('|', '/')} | {cc} | {genre} | {odd}".rstrip(" |"))
        print(f"проверено {min(i, len(queue))}, добавлено {len(added)}, отклонено {rejected}")

with open(OUT, "w", encoding="utf-8") as f:
    f.write("# Добавлено автоматически с Last.fm (scripts/expand_lastfm.py). Можно править руками.\n")
    f.write("\n".join(added) + "\n")
json.dump(tracks, open(TRACKS, "w", encoding="utf-8"), ensure_ascii=False, indent=0, sort_keys=True)
log.insert(0, f"Добавлено групп: {len(added)} (русских кандидатов {len(ru)}), отклонено {rejected}, по жанрам: {per_genre}")
open(REPORT, "w", encoding="utf-8").write("\n".join(log) + "\n")
done.append(cfg)
json.dump(done, open(DONE, "w", encoding="utf-8"), ensure_ascii=False)
print(log[0])
