# yt2drive

Give it a YouTube playlist URL. It downloads every track as tagged audio with cover art and puts it in a Google Drive folder. Run it again next week and it only fetches what's new.

```bash
yt2drive sync "https://www.youtube.com/playlist?list=PL..." --dest ~/Drive/Music
```

```
Playlist: Late night mix  (58 videos)
  already have: 51   duplicates skipped: 0   to download: 7

     [1/7] ok   Some Track Name  (4.1 MB)
     [2/7] ok   Another One  (3.8 MB)
     ...
Done. 7 new (28.4 MB), 51 already present, 0 duplicates, 0 failed.
```

---

## Quick start (Colab — recommended)

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/faisal-saddique/yt2drive/blob/main/notebooks/yt2drive_colab.ipynb)

Click the badge, paste your playlist URL, run the cells.

This is the easiest path for two reasons:

- **No auth setup.** Colab mounts your Drive with one approval popup. No GCP project, no OAuth consent screen, no service account JSON, no `rclone config`. Nothing is stored anywhere.
- **It's fast and costs you nothing.** The download from YouTube and the write to Drive both happen inside Google's network. Your home connection is never in the path, so a 300-track playlist finishes at datacenter speed while your laptop does nothing.

Don't want Drive at all? Step 2 in the notebook has a **"Colab local storage"** option — no Google account needed. Files stay on the Colab VM's disk and the last cell zips them up and downloads the zip through your browser. That disk is wiped when the runtime recycles, so it's only for pulling the zip down, not long-term storage.

The tradeoff is that Colab runs on datacenter IPs, which YouTube sometimes challenges — see [bot checks](#when-youtube-asks-you-to-prove-youre-not-a-bot).

## Quick start (your own machine)

Needs [uv](https://docs.astral.sh/uv/getting-started/installation/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`).

```bash
uv tool install git+https://github.com/faisal-saddique/yt2drive.git

# ffmpeg is required
sudo apt install ffmpeg        # Debian/Ubuntu
brew install ffmpeg            # macOS
winget install Gyan.FFmpeg     # Windows

yt2drive sync "PLAYLIST_URL" --dest ~/"Google Drive/My Drive/Music"
```

Point `--dest` at a folder that the Google Drive desktop app already syncs and you're done — no API credentials at all. If you'd rather push to Drive directly, `rclone copy` the destination folder afterwards; the manifest travels with the folder either way.

Running locally has one real advantage: your residential IP essentially never gets bot-checked.

---

## How the deduplication works

Re-running a sync must be cheap and must never produce a second copy of anything. Three independent layers make that true:

**1. Video ID is the primary key.** Every file is saved as `Title [dQw4w9WgXcQ].m4a`, and the manifest is keyed by that ID. Renaming a video on YouTube, reordering the playlist, or downloading the same track from two different playlists all resolve to the same key.

**2. The manifest lives inside the destination folder.** State is at `<dest>/.yt2drive/manifest.json` — inside Drive, next to the audio. A fresh Colab session, a different laptop, or a reinstall all pick up exactly where the last run stopped, because the dedup state is part of the library rather than part of the machine.

**3. The filesystem is the ultimate source of truth.** Before each sync, `reconcile()` scans the destination and parses the `[videoid]` suffix off every audio file. Delete the manifest, add files by hand, restore from a backup — the state rebuilds itself. It also works the other way: if a file was deleted behind the tool's back, the manifest notices and re-queues it.

Optionally, `--dedupe-by-title` catches the *same track uploaded twice under different IDs*, by normalising away the decoration:

```
"Artist - Song (Official Music Video)"  ─┐
"Artist - Song [HD] (Lyrics)"           ─┼─→  same key, downloaded once
"Artist - Song"                         ─┘
```

It's off by default because it's deliberately lossy — it can occasionally collapse a studio version and a live version into one entry.

## What makes it fast

- **Flat playlist enumeration.** A re-sync reads the playlist with `extract_flat`, which is ~2 requests for the whole thing, instead of running the full extractor on all 200 videos. This is both the speed win and the reason repeated runs don't trip rate limiting.
- **Two levels of parallelism.** `--workers` videos at a time, each split into `--fragments` parallel chunks.
- **No transcoding by default.** YouTube already serves AAC (~128 kbps) next to Opus, so asking for `m4a` makes ffmpeg do a stream *copy*. No CPU spent re-encoding, and no generational quality loss.
- **Staged writes.** Downloads complete and get tagged on local disk, then move into the destination in one atomic operation. On a Drive FUSE mount this matters a lot: Drive only ever sees finished audio, and a killed session leaves no half-files behind.
- **Progress saved per track.** Interrupt at any point; re-run resumes.

## Audio formats

| `--format` | Codec | Re-encodes? | Notes |
|---|---|---|---|
| `m4a` *(default)* | AAC ~128k | **No** | Best size-to-quality with no quality loss. Plays on essentially everything. |
| `opus` | Opus ~110k | No | ~25% smaller again. Not supported by older hardware players. |
| `mp3` | MP3 V0 | Yes | The compatibility floor. Transcoding from YouTube's audio loses a little quality. |

`m4a` is the default because it was the only option that satisfies "best available, runs everywhere, and small" at the same time. `flac` is deliberately not offered — YouTube's source audio is already lossy, so it would produce files ten times the size with zero quality gain.

## Commands

```bash
# download everything new
yt2drive sync URL --dest FOLDER

# several playlists; tracks shared between them download once
yt2drive sync URL_A URL_B --dest FOLDER

# see what would happen, download nothing
yt2drive sync URL --dest FOLDER --dry-run

# what's in the library
yt2drive status --dest FOLDER --failures

# rebuild the manifest from the files on disk
yt2drive verify --dest FOLDER
```

### Useful flags

| Flag | Effect |
|---|---|
| `-w, --workers N` | Videos in parallel (default 3). Lower it if you're being rate-limited. |
| `--fragments N` | Parallel chunks per video (default 4). |
| `--dedupe-by-title` | Also skip re-uploads of a track you already have. |
| `--retry-failed` | Retry entries marked unavailable or past the attempt cap. |
| `--cookies FILE` | Use an exported `cookies.txt` (fixes bot checks). |
| `--cookies-from-browser chrome` | Pull cookies live from your local browser. |
| `--limit N` | Only consider the first N playlist entries. |
| `--rate-limit 2M` | Cap download speed. |
| `--sleep 2` | Pause between videos to stay under rate limits. |
| `--no-thumbnail` / `--no-metadata` | Skip cover art / tags. |

## Failure handling

Failures are classified rather than lumped together:

- **Transient** (network blip, 503, ffmpeg hiccup) — retried automatically on the next sync, up to 5 attempts, then parked until `--retry-failed`.
- **Permanent** (private, deleted, region-blocked, terminated account) — recorded once and never requested again, so a playlist with a dozen dead videos doesn't waste a dozen requests on every run.
- **Bot check / HTTP 429** — the run aborts immediately. Hammering a rate-limited endpoint makes the block worse, so it stops and tells you to supply cookies.

A single failure never stops the batch; everything else in the playlist still downloads.

## When YouTube asks you to prove you're not a bot

Datacenter IPs get challenged. Give it a signed-in session:

1. Install a **"Get cookies.txt LOCALLY"** browser extension.
2. Open `youtube.com` signed in, export `cookies.txt`.
3. Pass `--cookies /path/to/cookies.txt` (or upload it in the notebook's cookies cell).

Running on your own machine, `--cookies-from-browser chrome` skips the export entirely.

Consider a throwaway Google account rather than handing a session cookie to a cloud VM. `.gitignore` already blocks `cookies.txt` from being committed — keep it that way.

If it still happens: drop to `--workers 1`, add `--sleep 2`, and make sure yt-dlp is current (`uv tool upgrade yt2drive` pulls the latest). An out-of-date yt-dlp is the single most common cause of extraction failures, because YouTube changes its player frequently.

## Scheduling it

Colab can't run unattended — it needs a browser tab open. For hands-off syncing, use cron on a machine that's always on:

```cron
0 3 * * * /usr/local/bin/yt2drive sync "PLAYLIST_URL" --dest /mnt/drive/Music --quiet >> /var/log/yt2drive.log 2>&1
```

GitHub Actions is possible but not recommended: its IP ranges are bot-checked constantly, so runs fail intermittently and you'd have to keep a cookie secret fresh.

## Development

```bash
git clone https://github.com/faisal-saddique/yt2drive.git
cd yt2drive
uv sync --extra dev
uv run pytest -q
```

The suite covers naming, manifest persistence and recovery, the dedup/diff logic, error classification, and an integration test that pushes a synthetic media file through the real yt-dlp + ffmpeg postprocessor chain over localhost.

## Layout

```
yt2drive/
├── naming.py       filename sanitising, title normalisation for dedup
├── manifest.py     the dedup ledger: atomic JSON, filesystem reconciliation
├── downloader.py   yt-dlp engine: playlist diffing, parallel fetch, tagging
└── cli.py          sync / status / verify
notebooks/
└── yt2drive_colab.ipynb
```

## Licence

MIT.

Download content you have the rights to. Fetching copyrighted material without a licence is against YouTube's Terms of Service — this tool doesn't change that, it just automates the mechanics for the cases where you're in the clear.
