# LessWrongTube

The YouTube videos that LessWrong keeps recommending, mined from 20 years of public posts and comments. No login and no tracking: likes, history and "for you" recommendations live in your browser.

Inspired by [HackerTube](https://hackertube.org), which did the same over Hacker News comments. HackerTube's code is not public; this is an independent implementation. Not affiliated with LessWrong or Lightcone.

## How it works

1. **Data.** No scraping: the pipeline reads a public crawl of LessWrong, [`x65617379/lesswrong_260509`](https://huggingface.co/datasets/x65617379/lesswrong_260509) on Hugging Face (45,933 posts and 906,338 comments, 2006 to May 2026, fetched from the public GraphQL API).
2. **Extract** (`pipeline/extract.py`). Every YouTube id linked from a post or a non-deleted comment becomes one row: who, when, karma, which thread. About 13,000 mentions of 10,600 videos by 2,160 people.
3. **Rank and enrich** (`pipeline/build.py`).
   - A video gets in if two or more different people linked it, or one person did in a post or comment with 15+ karma.
   - Score = distinct people + 0.5 * ln(1 + total karma). One enthusiast linking the same talk ten times is one vote.
   - Titles and channels come from YouTube's oEmbed endpoint (no API key). Private and deleted videos drop out there.
   - "Shared alongside" links videos that appear in the same thread (weight 1) or are linked by the same people (weight 0.5), damped by group size.
   - Each video keeps up to three quotes, one per person, with the text around the link, so you can see why it was shared, and whether it was praise or critique.
4. **Site** (`site/`). One static `index.html` plus `data.json`. Thumbnails are YouTube's automatic frame from the middle of the video (`hq2.jpg`), not the uploader's A/B-tested thumbnail.

## Run it

Needs [uv](https://docs.astral.sh/uv/).

```bash
uv run --with huggingface_hub python -c "from huggingface_hub import snapshot_download as s; s('x65617379/lesswrong_260509', repo_type='dataset', local_dir='data/hf')"
uv run pipeline/extract.py      # data/mentions.parquet
uv run pipeline/build.py        # site/data.json (oEmbed results cached in data/oembed.jsonl)
python -m http.server -d site   # http://localhost:8000
```

Pushing changes under `site/` deploys to GitHub Pages.

## Ideas

- Classify each mention as recommend / critique / just cite with an LLM, and rank on recommendations only.
- Durations through the YouTube Data API, for a "longs" shelf.
- The same pipeline for books, papers and LessWrong's own most-cited posts.
- Effective Altruism Forum and Alignment Forum run on the same software, so the same crawler works.

## License

Code: MIT. Quotes and data derived from LessWrong content are CC BY 4.0, credited to their authors via the linked comments.
