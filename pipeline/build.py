# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.1", "httpx>=0.27"]
# ///
"""Rank the mentioned videos, enrich them via oEmbed and write site/data.json.

Ranking counts distinct people, not mentions: one enthusiast linking the same
talk ten times is one vote. Karma is a tiebreaker on a log scale, so a single
+200 comment cannot outweigh several independent recommendations.

oEmbed needs no API key and answers 401/404 for private or deleted videos,
which doubles as the dead-link filter. Results are cached in data/oembed.jsonl
so reruns only hit YouTube for new ids.
"""

import html
import json
import math
import pathlib
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations

import duckdb
import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
MENTIONS = ROOT / "data" / "mentions.parquet"
CACHE = ROOT / "data" / "oembed.jsonl"
OUT = ROOT / "site" / "data.json"

# Videos linked as jokes rather than recommendations.
BLOCKLIST = {"dQw4w9WgXcQ"}
MIN_PEOPLE = 2
MIN_KARMA_SOLO = 15  # a single mention still counts if the community upvoted it
NEIGHBORS = 12
QUOTES = 3

TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


def plain(body: str, video_id: str, width: int = 320) -> str:
    """Text around the first link to the video, so the quote explains why it was shared."""
    i = body.find(video_id)
    if i >= 0:
        # the id usually sits inside <a href="...">; cut at the tag start, not mid-attribute
        lt, gt = body.rfind("<", 0, i), body.rfind(">", 0, i)
        if lt > gt:
            i = lt
    before = TAG.sub(" ", body[:i]) if i >= 0 else ""
    after = TAG.sub(" ", body[i:]) if i >= 0 else TAG.sub(" ", body)
    before = WS.sub(" ", html.unescape(before)).strip()
    after = WS.sub(" ", html.unescape(after)).strip()
    text = (before[-width // 2 :] + " " + after).strip()
    text = re.sub(r"https?://\S+", "", text)
    text = WS.sub(" ", text).strip()
    if len(text) > width:
        text = text[:width].rsplit(" ", 1)[0] + "…"
    if before and len(before) > width // 2:
        text = "…" + text
    return text


def load_cache() -> dict:
    if not CACHE.exists():
        return {}
    out = {}
    for line in CACHE.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        out[row["id"]] = row
    return out


def enrich(ids: list[str]) -> dict:
    cache = load_cache()
    todo = [i for i in ids if i not in cache]
    print(f"oembed: {len(ids) - len(todo)} cached, {len(todo)} to fetch")
    client = httpx.Client(timeout=15, headers={"User-Agent": "lesswrongtube/0.1"})

    def fetch(vid: str) -> dict | None:
        url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={vid}&format=json"
        for attempt in range(4):
            try:
                r = client.get(url)
            except httpx.HTTPError:
                time.sleep(2**attempt)
                continue
            if r.status_code == 200:
                d = r.json()
                return {"id": vid, "ok": True, "title": d.get("title"), "channel": d.get("author_name"),
                        "channel_url": d.get("author_url")}
            if r.status_code in (400, 401, 403, 404):
                return {"id": vid, "ok": False, "status": r.status_code}
            time.sleep(2**attempt)  # 429/5xx: back off
        return None  # retried next run

    with CACHE.open("a", encoding="utf-8") as fh, ThreadPoolExecutor(6) as pool:
        for n, row in enumerate(pool.map(fetch, todo), 1):
            if row:
                cache[row["id"]] = row
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
            if n % 250 == 0:
                print(f"  {n}/{len(todo)}", flush=True)
    client.close()
    return cache


def main() -> None:
    con = duckdb.connect()
    con.sql(f"create view m as select * from '{MENTIONS.as_posix()}'")
    stats = con.sql(
        """
        select video_id,
               count(distinct coalesce(user_id, src_id)) people,
               count(*) mentions,
               sum(greatest(score, 0)) karma,
               max(score) best,
               min(posted_at) first_seen,
               max(posted_at) last_seen
        from m group by 1
        """
    ).fetchall()
    cand = {
        v: dict(people=p, mentions=n, karma=k, best=b, first=f[:10], last=l[:10])
        for v, p, n, k, b, f, l in stats
        if v not in BLOCKLIST and (p >= MIN_PEOPLE or (b or 0) >= MIN_KARMA_SOLO)
    }
    print(f"{len(stats)} videos mentioned, {len(cand)} candidates")

    meta = enrich(sorted(cand))
    live = {v: s for v, s in cand.items() if meta.get(v, {}).get("ok")}
    print(f"{len(live)} still online")

    rows = con.sql(
        "select video_id, kind, src_url, post_id, post_title, post_url, user_id, user_name, score, posted_at, body from m"
    ).fetchall()
    quotes = defaultdict(list)
    posts_of = defaultdict(set)
    users_of = defaultdict(set)
    threads = {}
    for vid, kind, src_url, post_id, post_title, post_url, uid, uname, score, posted, body in rows:
        if vid not in live:
            continue
        posts_of[vid].add(post_id)
        if uid:
            users_of[vid].add(uid)
        threads[post_id] = (post_title, post_url)
        quotes[vid].append(
            dict(text=plain(body or "", vid), user=uname or "[deleted]", karma=score or 0,
                 url=src_url, date=(posted or "")[:10], kind=kind, post=post_title)
        )

    # Item-item similarity: videos shared in the same thread or by the same people.
    by_post, by_user = defaultdict(set), defaultdict(set)
    for vid in live:
        for p in posts_of[vid]:
            by_post[p].add(vid)
        for u in users_of[vid]:
            by_user[u].add(vid)
    sim = defaultdict(lambda: defaultdict(float))
    for group, weight in ((by_post, 1.0), (by_user, 0.5)):
        for members in group.values():
            if len(members) < 2 or len(members) > 60:  # mega-threads link everything to everything
                continue
            w = weight / math.log2(1 + len(members))
            for a, b in combinations(sorted(members), 2):
                sim[a][b] += w
                sim[b][a] += w

    videos = []
    for vid, s in live.items():
        score = s["people"] + 0.5 * math.log1p(s["karma"] or 0)
        q = sorted(quotes[vid], key=lambda x: -x["karma"])
        # one quote per person
        seen, top = set(), []
        for x in q:
            if x["user"] in seen or not x["text"]:
                continue
            seen.add(x["user"])
            top.append(x)
            if len(top) == QUOTES:
                break
        thread_list = sorted(posts_of[vid], key=lambda p: threads[p][0] or "")[:5]
        videos.append(
            dict(
                id=vid,
                title=meta[vid]["title"],
                channel=meta[vid]["channel"],
                channel_url=meta[vid].get("channel_url"),
                score=round(score, 3),
                people=s["people"],
                mentions=s["mentions"],
                karma=s["karma"],
                first=s["first"],
                last=s["last"],
                quotes=top,
                threads=[dict(title=threads[p][0], url=threads[p][1]) for p in thread_list],
                # co-mention strength, tilted toward neighbors many people vouched for
                related=[
                    v
                    for v, _ in sorted(
                        sim[vid].items(), key=lambda kv: -kv[1] * math.log2(1 + live[kv[0]]["people"])
                    )[:NEIGHBORS]
                ],
            )
        )
    videos.sort(key=lambda v: -v["score"])
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(
        json.dumps(
            dict(
                generated=time.strftime("%Y-%m-%d"),
                source="huggingface.co/datasets/x65617379/lesswrong_260509",
                videos=videos,
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    print(f"wrote {OUT} with {len(videos)} videos ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
