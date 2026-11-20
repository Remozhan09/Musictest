"""Находит YouTube-ролики для групп из site/bands.json.

Результат: site/videos.json = {"Группа": {"v": "<id>", "t": "<название ролика>"}}
- Если трек указан — ищет клип на него, иначе самый популярный клип группы.
- Берёт ролик, где в названии или канале есть название группы (чтобы не попалась чужая группа
  с похожим названием), без каверов, реакций и полных альбомов, и который можно встроить.
- Уже найденные не ищет повторно, поэтому скрипт можно запускать много раз.
- Чтобы перенайти ролик группы, удали её из videos.json.
"""
import json, os, re, sys, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import yt_dlp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "site")
OUT = os.path.join(SITE, "videos.json")
BUDGET_MIN = float(os.environ.get("BUDGET_MIN", "300"))
WORKERS = int(os.environ.get("WORKERS", "6"))
CIS = {"ru", "ua", "by", "kz", "md"}

bands = json.load(open(os.path.join(SITE, "bands.json"), encoding="utf-8"))
videos = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else {}
lock = threading.Lock()
start = time.time()
session = requests.Session()
session.headers["User-Agent"] = "Mozilla/5.0"
YDL_OPTS = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
BAD = re.compile(r"\b(cover|reaction|реакция|разбор|кавер|full album|полный альбом|karaoke|instrumental|"
                 r"tutorial|guitar lesson|drum cam|playthrough|interview|интервью|podcast|tribute)\b", re.I)


def norm(s):
    return re.sub(r"[\W_]+", "", (s or "").lower().replace("ё", "е"))


def embeddable(vid):
    try:
        r = session.get("https://www.youtube.com/oembed",
                        params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"}, timeout=15)
        return r.status_code == 200
    except Exception:
        return False


def search(query, band, track):
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(f"ytsearch8:{query}", download=False)
    nb, nt = norm(band), norm(track)
    cands = []
    for rank, e in enumerate((info or {}).get("entries") or []):
        vid, dur, title = e.get("id"), e.get("duration"), e.get("title") or ""
        if not vid or len(vid) != 11 or (dur and dur > 900) or BAD.search(title):
            continue
        hay = norm(title + " " + (e.get("channel") or e.get("uploader") or ""))
        if nb and nb not in hay:
            continue
        low = title.lower()
        # без трека: название должно выглядеть как музыкальное ("Группа - Песня", official, клип...)
        if not nt and not re.search(r"\s[-–—|:]\s|official|клип|music video|lyric|audio|«|\"", low):
            continue
        score = -rank
        if "official" in low or "клип" in low or "music video" in low:
            score += 4
        if nt and nt in norm(title):
            score += 6
        if re.search(r"\blive\b|концерт|\bживьём\b", low):
            score -= 5
        cands.append((score, vid, title))
    for _, vid, title in sorted(cands, reverse=True):
        if embeddable(vid):
            return vid, title
    return None, None


def task(b):
    band, track, cc = b[0], b[1], b[2]
    if track:
        queries = [f"{band} {track} official video", f"{band} {track}"]
    elif cc in CIS:
        queries = [f"{band} клип", f"{band} official video", f"{band} группа"]
    else:
        queries = [f"{band} official music video", f"{band} band"]
    for q in queries:
        for attempt in range(2):
            try:
                vid, title = search(q, band, track)
                if vid:
                    return band, vid, title
                break
            except Exception as ex:
                print(f"  ! {band}: {ex}", file=sys.stderr)
                time.sleep(5 * (attempt + 1))
    return band, None, None


def save():
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(videos, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, OUT)


def stale(b):
    v = videos.get(b[0], {})
    if not v.get("v"):
        return True
    # трек появился позже (например, с Last.fm), а найденный ролик — про другую песню
    return bool(b[1]) and norm(b[1]) not in norm(v.get("t", "")) and not v.get("x")


jobs = [b for b in bands if stale(b)]
print(f"Нужно найти: {len(jobs)}, уже есть: {len(bands) - len(jobs)}")
found = miss = 0
with ThreadPoolExecutor(WORKERS) as pool:
    futs = [pool.submit(task, b) for b in jobs]
    for i, f in enumerate(as_completed(futs), 1):
        band, vid, title = f.result()
        with lock:
            if vid:
                videos[band] = {"v": vid, "t": title}
                found += 1
            elif videos.get(band, {}).get("v"):
                videos[band]["x"] = 1  # трек не нашёлся — оставляем прежний ролик
            else:
                miss += 1
                print(f"  - не найдено: {band}")
            if i % 25 == 0:
                save()
                print(f"[{i}/{len(jobs)}] найдено {found}, не найдено {miss}, {int(time.time()-start)} c")
        if (time.time() - start) / 60 > BUDGET_MIN:
            print("Лимит времени, сохраняю и выхожу")
            for x in futs:
                x.cancel()
            break
save()
total = sum(1 for b in bands if videos.get(b[0], {}).get("v"))
print(f"Готово: найдено {found}, не найдено {miss}. Всего с видео: {total} из {len(bands)}")
