<picture>
  <source media="(prefers-color-scheme: dark)" srcset="card-dark.svg?v=2">
  <img src="card-light.svg?v=2" width="100%" alt="Podcassum stats: episodes summarised per category">
</picture>

# Podcassum

**Podcast + sum(mary).** Rhymes with [*possum*](https://de.wikipedia.org/wiki/Nordopossum), so it got the job, listening in so you don't have to.

Podcassum checks a list of podcasts every day. For each new episode it gets the full text and has Claude write a short summary with a verdict: **listen**, **skim** (with timestamps of the parts worth hearing) or **skip**, judged against a written list of interests.

## How it works

1. A GitHub Actions workflow runs `podcassum.py` every day.
2. The script reads the podcast feeds. For each new episode it uses the podcast's own transcript when there is one, and otherwise turns the MP3 into text with [faster-whisper](https://github.com/SYSTRAN/faster-whisper).
3. It starts a Claude Code [scheduled task](https://code.claude.com/docs/en/routines.md) through its API, once per episode. Claude reads the transcript and writes the summary.
4. The script redraws the card above from the totals so far.

The podcast list, interests, transcripts and summaries live in a separate private repository. This repository holds only the code and the card.

## Running it yourself

- A private data repository, checked out to `data/`, with:
  - `podcasts.toml`: one `[[podcast]]` table per podcast, with `id`, `name`, `category`, `feed`, and `vtt_from_link = true` when the transcript is at the episode's link plus `.vtt`
  - `interests.md`: what you care about, and how to give the verdict
  - `task-prompt.md`: the prompt for the Claude task
  - `README.md`, written by the script: an index of every summarised episode with its verdict and links to the summary and the Claude session
  - `logs/`, written by the script: the workflow log of a public repository is public too, so it shows only counts and the details go here
- A Claude Code scheduled task working in that repository, with `task-prompt.md` as its prompt and an API trigger.
- Repository secrets: `CLAUDE_TASK_URL` and `CLAUDE_TASK_TOKEN` (the task's API trigger), and `DATA_DEPLOY_KEY` (an SSH deploy key with write access to the data repository).

Run it locally without committing or starting Claude: `PODCASSUM_DATA=../podcassum-data uv run podcassum.py --no-git --no-fire`. Episodes without a published transcript go through Whisper, which keeps every CPU core busy for minutes per episode.
