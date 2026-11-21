"""Расширяет список групп с Last.fm и находит лучший трек каждой группы.

Работает через официальный API Last.fm: нужен ключ в секрете репозитория LASTFM_KEY
(HTML-страницы Last.fm серверам GitHub не отдаёт). Без ключа шаг просто пропускается.
1. Берёт кандидатов из tag.getTopArtists (русские теги — в первую очередь).
2. Для каждой группы: artist.getInfo (слушатели, теги) и artist.getTopTracks (топ-трек №1).
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

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CFG = os.path.join(DATA, "lastfm.json")
DONE = os.path.join(DATA, "lastfm_done.json")
OUT = os.path.join(DATA, "zz_lastfm.txt")
TRACKS = os.path.join(DATA, "lastfm_tracks.json")
REPORT = os.path.join(DATA, "lastfm_report.txt")

# тег -> жанр; порядок = приоритет при сборе кандидатов
# только русская сцена: русское эмо и всё в духе A-One нулевых
RU_TAGS = [("russian emo", "emo"), ("emo russian", "emo"), ("русское эмо", "emo"), ("эмо", "emo"),
           ("russian emocore", "emo"), ("russian post-hardcore", "phc"), ("пост-хардкор", "phc"),
           ("russian screamo", "scr"), ("скримо", "scr"), ("russian pop-punk", "pp"), ("russian pop punk", "pp"),
           ("поп-панк", "pp"), ("russian alternative", "alt"), ("russian alternative metal", "alt"),
           ("russian nu metal", "alt"), ("ню-метал", "alt"), ("альтернатива", "alt")]
TAGS = []
GENRE_TAGS = {"emo": "emo", "screamo": "scr", "skramz": "scr", "emocore": "emo", "midwest emo": "emo",
              "post-hardcore": "phc", "metalcore": "mc", "melodic metalcore": "mc", "deathcore": "dc",
              "mathcore": "math", "pop punk": "pp", "pop-punk": "pp", "easycore": "pp",
              "alternative metal": "alt", "nu metal": "alt", "hardcore": "mc", "melodic hardcore": "phc",
              "эмо": "emo", "русское эмо": "emo", "russian emo": "emo", "скримо": "scr", "пост-хардкор": "phc",
              "russian post-hardcore": "phc", "поп-панк": "pp", "russian pop-punk": "pp", "ню-метал": "alt",
              "russian nu metal": "alt", "russian alternative": "alt", "альтернатива": "alt", "металкор": "mc"}
BAD_TAGS = {"hip-hop", "hip hop", "rap", "emo rap", "trap", "cloud rap", "pop", "electronic", "rnb", "r&b",
            "dance", "edm", "hyperpop", "russian rap", "russian hip-hop", "phonk", "witch house", "k-pop"}
COUNTRY_TAGS = [("russian", "ru"), ("russia", "ru"), ("ukrainian", "ua"), ("ukraine", "ua"),
                ("belarusian", "by"), ("belarus", "by"), ("kazakh", "kz"), ("american", "us"), ("usa", "us"),
                ("british", "uk"), ("uk", "uk"), ("english", "uk"), ("welsh", "uk"), ("scottish", "uk"),
                ("canadian", "ca"), ("australian", "au"), ("german", "de"), ("japanese", "jp"), ("swedish", "se"),
                ("french", "fr"), ("finnish", "fi"), ("brazilian", "br"), ("mexican", "mx"), ("polish", "pl"),
                ("italian", "it"), ("dutch", "nl"), ("danish", "dk"), ("norwegian", "no")]

KEY = os.environ.get("LASTFM_KEY", "").strip()
API = "https://ws.audioscrobbler.com/2.0/"
if not KEY:
    print("Нет ключа LASTFM_KEY — расширение с Last.fm пропускаю"); sys.exit(0)
if not os.path.exists(CFG):
    print("Нет data/lastfm.json — пропускаю"); sys.exit(0)
cfg = json.load(open(CFG, encoding="utf-8"))
done = json.load(open(DONE, encoding="utf-8")) if os.path.exists(DONE) else []
if cfg in done:
    print("Это расширение уже сделано — пропускаю"); sys.exit(0)
TARGET = cfg.get("target_total", 2000)
PER_TAG = cfg.get("per_tag", 400)
RU_PER_TAG = cfg.get("ru_per_tag", 500)
RU_MIN = cfg.get("ru_min_listeners", 300)
MIN = cfg.get("min_listeners", 3000)
CAPS = cfg.get("caps", {"pp": 200, "alt": 60})
SKIP_GENRES = set(cfg.get("skip_genres", ["dc"]))

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/124.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"})
lock = threading.Lock()
log = []


def norm(s):
    return re.sub(r"[\W_]+", "", (s or "").lower().replace("ё", "е"))


def num(s):
    s = re.sub(r"[^\d]", "", s or "")
    return int(s) if s else 0


def country(name, tags, default=""):
    if re.search(r"[а-яё]", name, re.I):
        default = default or "ru"
    for t in tags[:10]:
        for word, cc in COUNTRY_TAGS:
            if re.search(rf"\b{re.escape(word)}\b", t):
                return cc
    return default


def api(method, **params):
    params.update(method=method, api_key=KEY, format="json")
    for attempt in range(4):
        try:
            r = S.get(API, params=params, timeout=25)
            if r.status_code == 200:
                d = r.json()
                if "error" in d:
                    if d["error"] in (6, 7):  # нет такого артиста/тега
                        return None
                    print(f"  ! {method}: {d.get('message')}", file=sys.stderr)
                    if d["error"] in (10, 26):  # неверный или заблокированный ключ
                        raise SystemExit("Ключ LASTFM_KEY не принят Last.fm: " + str(d.get("message")))
                    time.sleep(3 * (attempt + 1)); continue
                return d
            print(f"  ! {method}: HTTP {r.status_code}", file=sys.stderr)
        except SystemExit:
            raise
        except Exception as ex:
            print(f"  ! {method}: {ex}", file=sys.stderr)
        time.sleep(3 * (attempt + 1))
    return None


def artist_info(name):
    """(теги, слушатели, топ-трек) или None"""
    d = api("artist.getinfo", artist=name, autocorrect=1)
    if not d or "artist" not in d:
        return None
    a = d["artist"]
    tags = [t["name"].lower() for t in ((a.get("tags") or {}).get("tag") or []) if isinstance(t, dict)]
    listeners = num(str((a.get("stats") or {}).get("listeners", "0")))
    t = api("artist.gettoptracks", artist=name, autocorrect=1, limit=1)
    tr = ((t or {}).get("toptracks") or {}).get("track") or []
    if isinstance(tr, dict):
        tr = [tr]
    track = tr[0]["name"].strip() if tr else ""
    time.sleep(0.25)
    return tags, listeners, track


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

# ---------- 1. кандидаты из тегов ----------
cands = {}  # norm -> dict


def tag_artists(tag, limit):
    out, page = [], 1
    while len(out) < limit:
        d = api("tag.gettopartists", tag=tag, limit=min(200, limit), page=page)
        arts = ((d or {}).get("topartists") or {}).get("artist") or []
        if not arts:
            break
        out += [a["name"] for a in arts if isinstance(a, dict) and a.get("name")]
        page += 1
        time.sleep(0.25)
    return out[:limit]


for group, tags in (("ru", RU_TAGS), ("intl", TAGS)):
    for tag, genre in tags:
        names = tag_artists(tag, RU_PER_TAG if group == "ru" else PER_TAG)
        for rank, name in enumerate(names):
            k = norm(name)
            if not k:
                continue
            c = cands.get(k)
            if c is None:
                cands[k] = {"name": name, "lis": 0, "genre": genre, "ru": group == "ru", "rank": rank, "n": len(cands)}
            else:
                c["ru"] = c["ru"] or group == "ru"
                c["rank"] = min(c["rank"], rank)
        log.append(f"тег «{tag}»: {len(names)}")
        print(log[-1])

if not cands:
    raise SystemExit("Last.fm: теги пустые — проверь ключ и доступ к API")

# ---------- 2. топ-треки для групп без трека ----------
tracks = {}


def fill_track(item):
    k, name = item
    c = cands.get(k)
    info = artist_info(c["name"] if c else name)
    if info and info[2]:
        with lock:
            tracks[name] = info[2]


with ThreadPoolExecutor(3) as pool:
    list(pool.map(fill_track, need_track.items()))
log.append(f"топ-треков для ручного списка: {len(tracks)} из {len(need_track)}")
print(log[-1])

# ---------- 3. новые группы ----------
new = [c for k, c in cands.items() if k not in have]
# порядок: русские, потом зарубежные; внутри — по месту в теге (теги идут по очереди)
ru = sorted([c for c in new if c["ru"]], key=lambda c: (c["rank"], c["n"]))
intl = sorted([c for c in new if not c["ru"]], key=lambda c: (c["rank"], c["n"]))
queue = ru + intl
want = TARGET - len(have)
print(f"Кандидатов: русских {len(ru)}, зарубежных {len(intl)}, нужно добавить {want}")
added, rejected, per_genre = [], 0, {}


def check(c):
    return c, artist_info(c["name"])


i = 0
with ThreadPoolExecutor(3) as pool:
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
            if lis < (RU_MIN if c["ru"] else MIN):
                rejected += 1
                continue
            top = tags[:4]
            rapish = any(t in ("emo rap", "cloud rap", "hip-hop", "hip hop", "rap", "trap", "russian rap") for t in tags[:3])
            if (top and top[0] in BAD_TAGS) or rapish:
                rejected += 1
                log.append(f"пропуск (не тот жанр: {', '.join(top)}): {c['name']}")
                continue
            genre = next((GENRE_TAGS[t] for t in tags[:6] if t in GENRE_TAGS), c["genre"])
            if genre in SKIP_GENRES:
                rejected += 1
                log.append(f"пропуск (жанр {genre}): {c['name']}")
                continue
            cap = CAPS.get(genre)
            if cap is not None and per_genre.get(genre, 0) >= cap:
                continue
            cc = country(c["name"], tags, "")
            if not re.search(r"[а-яё]", c["name"], re.I) and not any(re.search(r"russia|русск", t) for t in tags[:10]):
                rejected += 1
                log.append(f"пропуск (не русская группа: {', '.join(top)}): {c['name']}")
                continue
            cc = "ru"
            per_genre[genre] = per_genre.get(genre, 0) + 1
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
