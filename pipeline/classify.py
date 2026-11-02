# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Label every video with a category, the community's stance and a one-line hook.

Batches of ~40 videos go to Claude in one call each, through the `claude` CLI
in print mode, so it runs on a Claude subscription without an API key. Each
call replaces Claude Code's system prompt and disables tools and MCP, which
cuts the fixed overhead from ~70k to ~1k tokens per call; the batch itself is
then almost all of the cost.

Labels are cached in data/labels.jsonl. Reruns only send unlabeled videos, so
an interrupted run just resumes. build.py merges the labels into data.json.

    uv run pipeline/classify.py                # everything missing, sonnet
    uv run pipeline/classify.py --limit 80     # pilot on the top 80
    uv run pipeline/classify.py --model haiku  # cheaper
"""

import argparse
import json
import pathlib
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "site" / "data.json"
LABELS = ROOT / "data" / "labels.jsonl"

CATEGORIES = {
    "ai-safety": "AI alignment, AI safety, AI risk and AI governance",
    "ai": "AI and machine learning: how models work, capabilities, the industry",
    "rationality": "Rationality, cognitive science, psychology, decision-making, epistemics",
    "math-cs": "Mathematics, statistics, computer science, programming",
    "science": "Physics, biology, chemistry, medicine research, astronomy, engineering",
    "philosophy": "Philosophy, ethics, consciousness, religion",
    "society": "Economics, politics, history, institutions, law, geopolitics",
    "futures": "Effective altruism, existential risk other than AI, longevity, transhumanism, space futures",
    "self": "Health, fitness, productivity, relationships, practical skills, meditation",
    "culture": "Fiction, film, music, art, games, humor",
    "other": "Anything that fits nowhere else",
}
STANCES = {
    "recommend": "the quotes mostly praise it or point people to it as worth watching",
    "reference": "it is cited as a source or example, without clear praise",
    "mixed": "some praise it, some criticize it",
    "critique": "it is mostly linked in order to disagree with it or as a bad example",
}

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "stance": {"type": "string", "enum": list(STANCES)},
                    "hook": {"type": "string"},
                },
                "required": ["id", "category", "stance", "hook"],
            },
        }
    },
    "required": ["items"],
}

SYSTEM = "You label YouTube videos for a catalog. Answer only through the structured output, one item per input id."


def prompt(batch: list[dict]) -> str:
    cats = "\n".join(f"- {k}: {v}" for k, v in CATEGORIES.items())
    stances = "\n".join(f"- {k}: {v}" for k, v in STANCES.items())
    items = [
        {
            "id": v["id"],
            "title": v["title"],
            "channel": v["channel"],
            "quotes": [q["text"][:240] for q in v["quotes"]],
        }
        for v in batch
    ]
    return f"""These YouTube videos were linked in LessWrong posts and comments. The quotes are the text around each link.

For each video give:
- category, the single best fit:
{cats}
- stance, how LessWrong treats the video, judged from the quotes:
{stances}
- hook: one plain sentence, at most 14 words, saying what the video offers a LessWrong reader. No hype, no "this video". Use the title and quotes; do not invent facts.

Videos:
{json.dumps(items, ensure_ascii=False)}"""


def run(batch: list[dict], model: str, claude: str) -> list[dict]:
    cmd = [
        claude, "-p", "--model", model, "--tools", "", "--strict-mcp-config",
        "--setting-sources", "", "--system-prompt", SYSTEM,
        "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
    ]
    r = subprocess.run(cmd, input=prompt(batch), capture_output=True, text=True, encoding="utf-8", timeout=600)
    if r.returncode:
        raise RuntimeError(r.stderr[-500:] or r.stdout[-500:])
    out = json.loads(r.stdout)
    wanted = {v["id"] for v in batch}
    items = [i for i in (out.get("structured_output") or {}).get("items", []) if i.get("id") in wanted]
    for i in items:
        i["model"] = model
    print(f"  batch of {len(batch)}: {len(items)} labels, ${out.get('total_cost_usd', 0):.4f}", flush=True)
    return items


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--batch", type=int, default=40)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, help="only the top N videos by score")
    args = ap.parse_args()

    claude = shutil.which("claude")
    if not claude:
        raise SystemExit("claude CLI not found on PATH")
    videos = json.loads(DATA.read_text(encoding="utf-8"))["videos"]
    if args.limit:
        videos = videos[: args.limit]
    done = set()
    if LABELS.exists():
        done = {json.loads(line)["id"] for line in LABELS.read_text(encoding="utf-8").splitlines() if line}
    todo = [v for v in videos if v["id"] not in done]
    batches = [todo[i : i + args.batch] for i in range(0, len(todo), args.batch)]
    print(f"{len(done)} labeled, {len(todo)} to go in {len(batches)} batches ({args.model})")

    labeled = 0
    with LABELS.open("a", encoding="utf-8") as fh, ThreadPoolExecutor(args.workers) as pool:
        futures = [pool.submit(run, b, args.model, claude) for b in batches]
        for f in futures:
            try:
                items = f.result()
            except Exception as e:  # one failed batch should not sink the run; rerun picks it up
                print(f"  batch failed: {e}", flush=True)
                continue
            for i in items:
                fh.write(json.dumps(i, ensure_ascii=False) + "\n")
            fh.flush()
            labeled += len(items)
    print(f"labeled {labeled}; missing ones are retried on the next run")


if __name__ == "__main__":
    main()
