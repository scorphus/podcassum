import argparse
import functools
import json
import os
import re
import subprocess
import tempfile
import time
import tomllib
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date
from email.utils import parsedate_to_datetime
from pathlib import Path

from faster_whisper import WhisperModel

import card

ROOT = Path(__file__).parent
# the private podcassum-data repo: podcasts.toml, episodes.json, transcripts/, summaries/
DATA = Path(os.environ.get("PODCASSUM_DATA", ROOT / "data"))
STATE = DATA / "episodes.json"
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
    if podcast.get("vtt_from_link") and episode["link"]:
        try:
            return vtt_to_text(fetch(episode["link"] + ".vtt").decode()), "the podcast's own transcript"
        except urllib.error.HTTPError:
            pass
    return transcribe(episode["mp3"]), f"Whisper speech-to-text ({WHISPER_MODEL} model); names and terms may be misheard"


def collect(podcasts, state):
    new = []
    for podcast in podcasts:
        try:
            episodes = parse_feed(fetch(podcast["feed"]))
        except Exception as error:
            print(f"::warning::{podcast['name']}: cannot read the feed: {error}")
            continue
        known = state.get(podcast["id"])
        if known is None:
            state[podcast["id"]] = {e["guid"]: {"title": e["title"], "published": e["published"], "baseline": True} for e in episodes}
            print(f"{podcast['name']}: first check, {len(episodes)} existing episodes marked as seen")
            continue
        new += [(podcast, e) for e in episodes if e["guid"] not in known]

    started = time.monotonic()
    for podcast, episode in sorted(new, key=lambda pe: pe[1]["published"]):
        if time.monotonic() - started > STOP_STARTING_AFTER_S:
            print(f"::warning::out of time, {podcast['name']} \"{episode['title']}\" waits for the next run")
            continue
        name = f"{episode['published']}-{slugify(episode['title'])}"
        transcript = f"transcripts/{podcast['id']}/{name}.txt"
        print(f"{podcast['name']}: \"{episode['title']}\"")
        try:
            text, source = episode_text(podcast, episode)
        except Exception as error:
            print(f"::warning::{podcast['name']} \"{episode['title']}\": no text: {error}")
            continue
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


def fire(state):
    for podcast_id, episodes in state.items():
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
                print(f"{episode['title']}: {episode['session']}")
            except urllib.error.HTTPError as error:
                print(f"::warning::{episode['title']}: Claude task not started, retrying next run: {error} {error.read().decode()[:300]}")


def save(state):
    STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False) + "\n")


def bump_card_version():
    readme = ROOT / "README.md"
    readme.write_text(re.sub(r"\?v=(\d+)", lambda m: f"?v={int(m.group(1)) + 1}", readme.read_text()))


def push(repo, paths, message):
    subprocess.run(["git", "add", *paths], cwd=repo, check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo).returncode == 0:
        return
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)
    subprocess.run(["git", "pull", "-q", "--rebase"], cwd=repo, check=True)
    subprocess.run(["git", "push", "-q"], cwd=repo, check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-git", action="store_true", help="do not commit or push")
    parser.add_argument("--no-fire", action="store_true", help="do not start Claude runs")
    args = parser.parse_args()

    podcasts = tomllib.loads((DATA / "podcasts.toml").read_text())["podcast"]
    state = json.loads(STATE.read_text()) if STATE.exists() else {}

    collect(podcasts, state)
    save(state)
    if not args.no_git:
        push(DATA, ["episodes.json", "transcripts"], "add new episode transcripts")
    if not args.no_fire:
        fire(state)
        save(state)
        if not args.no_git:
            push(DATA, ["episodes.json"], "record started Claude runs")

    if card.write(podcasts, state, DATA, ROOT, date.today()):
        bump_card_version()
    if not args.no_git:
        push(ROOT, ["card-dark.svg", "card-light.svg", "README.md"], "update the stats card")


if __name__ == "__main__":
    main()
