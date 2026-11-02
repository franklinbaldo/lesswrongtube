# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.1"]
# ///
"""Pull every YouTube mention out of the LessWrong dump into one flat table.

One row per (video, post-or-comment). The ranking step needs who mentioned
it, when, with what karma and in which thread, so we keep those fields and
leave the HTML behind (only the bodies of rows that mention a video are kept,
for later context snippets).
"""

import pathlib

import duckdb

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "hf" / "data" / "raw" / "*.parquet"
OUT = ROOT / "data" / "mentions.parquet"

# 11-char YouTube ids from watch/embed/shorts/live/youtu.be links.
YT = (
    r"(?:youtube(?:-nocookie)?\.com/(?:watch\?(?:[^\"'\s<>]*?&(?:amp;)?)?v=|embed/|shorts/|live/|v/)"
    r"|youtu\.be/)([A-Za-z0-9_-]{11})"
)

YT_SQL = YT.replace("'", "''")

SQL = f"""
with posts as (
  select post._id as post_id, post.title as post_title, post.pageUrl as post_url,
         post.user._id as user_id, post.user.displayName as user_name,
         post.baseScore as score, post.postedAt as posted_at,
         coalesce(post.htmlBody, '') || ' ' || coalesce(post.url, '') as body,
         'post' as kind, post._id as src_id, post.pageUrl as src_url
  from read_parquet('{RAW.as_posix()}')
),
comments as (
  select p.post._id as post_id, p.post.title as post_title, p.post.pageUrl as post_url,
         c.user._id as user_id, c.user.displayName as user_name,
         c.baseScore as score, c.postedAt as posted_at,
         coalesce(c.htmlBody, '') as body,
         'comment' as kind, c._id as src_id, c.pageUrl as src_url
  from read_parquet('{RAW.as_posix()}') p, unnest(p.comments) as t(c)
  where not coalesce(c.deleted, false)
),
docs as (select * from posts union all select * from comments),
hits as (
  select *, unnest(list_distinct(regexp_extract_all(body, '{YT_SQL}', 1))) as video_id
  from docs
  where body ilike '%youtu%'
)
select video_id, kind, src_id, src_url, post_id, post_title, post_url,
       user_id, user_name, score, posted_at, body
from hits
"""


def main() -> None:
    con = duckdb.connect()
    con.sql(f"copy ({SQL}) to '{OUT.as_posix()}' (format parquet)")
    print(
        con.sql(
            f"""select count(*) mentions, count(distinct video_id) videos,
                       count(distinct user_id) users
                from '{OUT.as_posix()}'"""
        ).fetchall()
    )


if __name__ == "__main__":
    main()
