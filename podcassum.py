import argparse
import functools
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
import traceback
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date
from email.utils import parsedate_to_datetime
from pathlib import Path

from faster_whisper import WhisperModel

import card

ROOT = Path(__file__).parent
# the private podcassum-data repo: podcasts.toml, episodes.json, transcripts/, summaries/
DATA = Path(os.environ.get("PODCASSUM_DATA", ROOT / "data"))
STATE = DATA / "episodes.json"
LOG = DATA / "logs" / f"{date.today().isoformat()}.log"
REQUESTS = DATA / "requests.toml"
REQUESTS_HELP = """\
# Episodes to summarise on request. The watcher handles each one on its next run and removes it here.
# Changing this file on main starts the watcher (.github/workflows/requests.yml).
#
# [[request]]
# podcast = "talkpython"   # the podcast's id in podcasts.toml
# episode = "552"          # the episode's number, or words that all appear in its title"""
NS = {
    "podcast": "https://podcastindex.org/namespace/1.0",
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
}
USER_AGENT = "podcassum (+https://github.com/scorphus/podcassum)"
WHISPER_MODEL = "small"
# leaves room in the 6-hour GitHub Actions job limit for the episode being transcribed
STOP_STARTING_AFTER_S = 5 * 3600


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].rstrip("-")


def minutes(duration):
    duration = (duration or "").strip()
    if ":" in duration:
        seconds = 0
        for part in duration.split(":"):
            seconds = seconds * 60 + int(part)
        return seconds // 60
    return int(duration) // 60 if duration.isdigit() else None


def parse_feed(xml):
    episodes = []
    for item in ET.fromstring(xml).find("channel").findall("item"):
        enclosure = item.find("enclosure")
        transcripts = {
            t.get("type"): t.get("url") for t in item.findall("podcast:transcript", NS)
        }
        title = item.findtext("title", "").strip()
        episodes.append(
            {
                "guid": item.findtext("guid") or item.findtext("link") or title,
                "title": title,
                "published": parsedate_to_datetime(item.findtext("pubDate")).date().isoformat(),
                "link": item.findtext("link"),
                "mp3": enclosure.get("url") if enclosure is not None else None,
                "minutes": minutes(item.findtext("itunes:duration", namespaces=NS)),
                "transcript_txt": transcripts.get("text/plain"),
                "transcript_vtt": transcripts.get("text/vtt"),
                "transcript_html": transcripts.get("text/html"),
            }
        )
    return episodes


def timestamp(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def vtt_to_text(vtt):
    lines, speaker, start = [], None, None
    for block in vtt.split("\n\n"):
        rows = block.strip().splitlines()
        cue = next((i for i, row in enumerate(rows) if "-->" in row), None)
        if cue is None:
            continue
        start = rows[cue].split("-->")[0].strip().split(".")[0]
        for row in rows[cue + 1 :]:
            match = re.match(r"<v ([^>]+)>(.*)", row)
            who, said = (match.group(1), match.group(2)) if match else (speaker, row)
            said = re.sub(r"<[^>]+>", "", said).strip()
            if who != speaker:
                lines.append(f"\n[{start}] {who}:")
                speaker = who
            lines.append(said)
    return " ".join(lines).replace(" \n", "\n").strip() + "\n"


def html_to_text(page):
    paragraphs = re.split(r"</p>|<br\s*/?>", page, flags=re.IGNORECASE)
    lines = (" ".join(html.unescape(re.sub(r"<[^>]+>", " ", p)).split()) for p in paragraphs)
    return "\n".join(line for line in lines if line) + "\n"


@functools.cache
def whisper():
    return WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=os.cpu_count())


def transcribe(mp3_url):
    with tempfile.NamedTemporaryFile(suffix=".mp3") as audio:
        audio.write(fetch(mp3_url))
        audio.flush()
        segments, _ = whisper().transcribe(audio.name, vad_filter=True)
        return "".join(f"[{timestamp(s.start)}] {s.text.strip()}\n" for s in segments)


def episode_text(podcast, episode):
    if episode["transcript_txt"]:
        return fetch(episode["transcript_txt"]).decode(), "the podcast's own transcript"
    if episode["transcript_vtt"]:
        return vtt_to_text(fetch(episode["transcript_vtt"]).decode()), "the podcast's own transcript"
    if episode["transcript_html"]:
        return html_to_text(fetch(episode["transcript_html"]).decode()), "the podcast's own transcript"
    if podcast.get("vtt_from_link") and episode["link"]:
        try:
            return vtt_to_text(fetch(episode["link"] + ".vtt").decode()), "the podcast's own transcript"
        except urllib.error.HTTPError:
            pass
    return transcribe(episode["mp3"]), f"Whisper speech-to-text ({WHISPER_MODEL} model); names and terms may be misheard"


def log(message):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {message}\n")


def matches(episode, wanted):
    if wanted.isdigit():
        return re.search(rf"(?<!\d){wanted}(?!\d)", episode["title"]) is not None
    return all(word in episode["title"].lower() for word in wanted.lower().split())


def load_requests():
    return tomllib.loads(REQUESTS.read_text()).get("request", []) if REQUESTS.exists() else []


def save_requests(requests):
    lines = [REQUESTS_HELP]
    for request in requests:
        lines += ["", "[[request]]", f'podcast = "{request["podcast"]}"', f'episode = "{request["episode"]}"']
    REQUESTS.write_text("\n".join(lines) + "\n")


def collect(podcasts, state, requests, counts):
    new, feeds = [], {}
    for podcast in podcasts:
        try:
            episodes = parse_feed(fetch(podcast["feed"]))
        except Exception as error:
            log(f"{podcast['name']}: cannot read the feed: {error}")
            counts["feeds that could not be read"] += 1
            continue
        feeds[podcast["id"]] = episodes
        known = state.get(podcast["id"])
        if known is None:
            state[podcast["id"]] = {e["guid"]: {"title": e["title"], "published": e["published"], "baseline": True} for e in episodes}
            log(f"{podcast['name']}: first check, {len(episodes)} existing episodes marked as seen")
            continue
        new += [(podcast, e) for e in episodes if e["guid"] not in known]
    counts["new episodes"] += len(new)

    by_id = {p["id"]: p for p in podcasts}
    pending, requested = [], []
    for request in requests:
        if request["podcast"] in by_id and request["podcast"] not in feeds:
            pending.append(request)
            continue
        found = [e for e in feeds.get(request["podcast"], []) if matches(e, str(request["episode"]))]
        if not found:
            log(f"request {request['podcast']} {request['episode']!r}: no such podcast or episode, dropped")
            counts["requests not found"] += 1
            continue
        podcast, episode = by_id[request["podcast"]], found[0]
        if "transcript" in state[podcast["id"]].get(episode["guid"], {}):
            log(f"request {request['podcast']} {request['episode']!r}: \"{episode['title']}\" is already summarised, dropped")
            continue
        log(f"request {request['podcast']} {request['episode']!r}: \"{episode['title']}\"")
        requested.append((request, podcast, episode))
        if all(e["guid"] != episode["guid"] for _, e in new):
            new.append((podcast, episode))
            counts["requested episodes"] += 1

    started = time.monotonic()
    for number, (podcast, episode) in enumerate(sorted(new, key=lambda pe: pe[1]["published"]), 1):
        progress = f"episode {number} of {len(new)}"
        if time.monotonic() - started > STOP_STARTING_AFTER_S:
            log(f"out of time, {podcast['name']} \"{episode['title']}\" waits for the next run")
            counts["episodes left for the next run"] += 1
            continue
        name = f"{episode['published']}-{slugify(episode['title'])}"
        transcript = f"transcripts/{podcast['id']}/{name}.txt"
        log(f"{progress}: {podcast['name']}: \"{episode['title']}\"")
        print(f"{progress}: getting the text")
        episode_started = time.monotonic()
        try:
            text, source = episode_text(podcast, episode)
        except Exception as error:
            log(f"{podcast['name']} \"{episode['title']}\": no text: {error}")
            print(f"{progress}: no text")
            counts["episodes without text"] += 1
            continue
        print(f"{progress}: text ready after {(time.monotonic() - episode_started) / 60:.1f} min")
        counts["transcripts"] += 1
        header = (
            f"Podcast: {podcast['name']}\nEpisode: {episode['title']}\nPublished: {episode['published']}\n"
            f"Link: {episode['link']}\nLength: {episode['minutes']} min\nText from: {source}\n\n"
        )
        (DATA / transcript).parent.mkdir(parents=True, exist_ok=True)
        (DATA / transcript).write_text(header + text)
        state[podcast["id"]][episode["guid"]] = {
            "title": episode["title"],
            "published": episode["published"],
            "transcript": transcript,
            "summary": f"summaries/{podcast['id']}/{name}.md",
            "header": header.strip(),
            "session": None,
        }

    return pending + [r for r, p, e in requested if "transcript" not in state[p["id"]][e["guid"]]]


def fire(state, counts):
    for episodes in state.values():
        for episode in episodes.values():
            if "transcript" not in episode or episode["session"]:
                continue
            text = f"{episode['header']}\nTranscript: {episode['transcript']}\nWrite the summary to: {episode['summary']}"
            request = urllib.request.Request(
                os.environ["CLAUDE_TASK_URL"],
                data=json.dumps({"text": text}).encode(),
                headers={
                    "Authorization": f"Bearer {os.environ['CLAUDE_TASK_TOKEN']}",
                    "anthropic-beta": "experimental-cc-routine-2026-04-01",
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    episode["session"] = json.load(response)["claude_code_session_url"]
                episode["delivered"] = date.today().isoformat()
                log(f"{episode['title']}: {episode['session']}")
                counts["Claude runs started"] += 1
            except Exception as error:
                body = error.read().decode()[:300] if isinstance(error, urllib.error.HTTPError) else ""
                log(f"{episode['title']}: Claude task not started, retrying next run: {error} {body}")
                counts["Claude runs that failed to start"] += 1


def save(state):
    STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False) + "\n")


def write_index(podcasts, state):
    names = {p["id"]: p["name"] for p in podcasts}
    rows = []
    for podcast_id, episodes in state.items():
        for episode in episodes.values():
            if not episode.get("session"):
                continue
            summary = DATA / episode["summary"]
            match = re.search(r"Verdict: (listen|skim|skip)", summary.read_text()) if summary.exists() else None
            verdict = f"**{match.group(1)}**" if match else "being written"
            title = episode["title"].replace("|", "·")
            link = f"[{title}]({episode['summary']})" if summary.exists() else title
            rows.append((episode["delivered"], episode["published"], f"| {episode['delivered']} | {names.get(podcast_id, podcast_id)} | {link} | {verdict} | [open]({episode['session']}) |"))
    rows.sort(reverse=True)
    lines = ["# Podcassum", "", "| Delivered | Podcast | Episode | Verdict | Claude |", "|---|---|---|---|---|", *(row for *_, row in rows)]
    (DATA / "README.md").write_text("\n".join(lines) + "\n")


def bump_card_version():
    readme = ROOT / "README.md"
    readme.write_text(re.sub(r"\?v=(\d+)", lambda m: f"?v={int(m.group(1)) + 1}", readme.read_text()))


def git(repo, *args):
    # git output names transcript files, so it goes to the private log, never to the public one
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if result.returncode:
        log(f"git {' '.join(args)} failed in {repo.name}:\n{result.stdout}{result.stderr}")
        raise RuntimeError("git failed")
    return result


def push(repo, paths, message):
    git(repo, "add", *paths)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo).returncode == 0:
        return
    git(repo, "commit", "-q", "-m", message)
    git(repo, "pull", "-q", "--rebase")
    git(repo, "push", "-q")


def run(args, counts):
    log("run started")
    podcasts = tomllib.loads((DATA / "podcasts.toml").read_text())["podcast"]
    state = json.loads(STATE.read_text()) if STATE.exists() else {}

    requests = load_requests()
    remaining = collect(podcasts, state, requests, counts)
    save(state)
    if remaining != requests:
        save_requests(remaining)
    if not args.no_git:
        push(DATA, ["episodes.json", "transcripts", "logs", *(["requests.toml"] if REQUESTS.exists() else [])], "add new episode transcripts")
    if not args.no_fire:
        fire(state, counts)
        save(state)
    write_index(podcasts, state)
    if not args.no_git:
        push(DATA, ["episodes.json", "logs", "README.md"], "record started Claude runs and update the index")

    if card.write(podcasts, state, DATA, ROOT, date.today()):
        bump_card_version()
    if not args.no_git:
        push(ROOT, ["card-dark.svg", "card-light.svg", "README.md"], "update the stats card")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-git", action="store_true", help="do not commit or push")
    parser.add_argument("--no-fire", action="store_true", help="do not start Claude runs")
    args = parser.parse_args()

    # this repository is public, and so are its workflow logs: print only counts, everything else goes to LOG
    counts = Counter()
    try:
        run(args, counts)
    except Exception:
        log(traceback.format_exc())
        print("::error::the run failed; the details are in the private log")
        if not args.no_git:
            try:
                push(DATA, ["logs"], "add the log of a failed run")
            except Exception:
                pass
        sys.exit(1)
    finally:
        for what in ("new episodes", "requested episodes", "transcripts", "Claude runs started"):
            print(f"{what}: {counts[what]}")
        for what in ("feeds that could not be read", "requests not found", "episodes without text", "episodes left for the next run", "Claude runs that failed to start"):
            if counts[what]:
                print(f"::warning::{what}: {counts[what]}")


if __name__ == "__main__":
    main()
