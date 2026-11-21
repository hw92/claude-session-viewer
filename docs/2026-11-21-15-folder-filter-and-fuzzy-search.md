# Folder filter and fuzzy search

## Folder filter

The folder dropdown was replaced by a box you type into, because 200 folders were too many to scroll through.

| Action | How |
|---|---|
| Open it | Press `f`, or click the box under the search bar |
| Find a folder | Type any part of the path, e.g. `tools sess`. Every word must appear in the path |
| Pick | Arrow keys + Enter, or click |
| Clear | `Esc`, or empty the box |
| Filter from an open session | Click the folder path in the session header |

Before you type, folders are listed with the most recently used first. Folders whose own name matches what you typed come first. Each line shows the folder name, its parent path and its session count.

## Fuzzy search

The "Search all sessions" box also matches misspelled words.

- A word gets corrected when it is 5 or more letters and is not a word in your sessions or the start of one. Shorter or partial words, like `sess`, are searched exactly as before.
- The search then also matches up to 5 real words that are 1 typo away (5-7 letters) or 2 typos away (8+). A swap of two neighboring letters counts as one typo.
- A word only counts as a replacement if it appears at least 3 times in your sessions, so your old typos are not offered.
- The status line shows the replacement, e.g. `94 sessions match. Also matching fortnue → fortune`, and the transcript highlights the replacement words too.

Limits: a typo in the first letter is not caught (`ortune` does not find `fortune`), and only words that start with a letter are corrected.

How it works: on startup, and after each rescan that finds changed files, the server reads every word in the index (about 94k words, under 1 s). A search with a misspelled word takes about 0.1-0.3 s.

## Ranked search

Search uses SQLite's full-text index (FTS5), so results are ranked and previews show where your words appear.

| You type | Matches |
|---|---|
| `docker supabase` | Sessions with both words. A word also matches longer words that start with it (`sess` finds *session*, `art` finds *artifact* but not *start*) |
| `"fortune alpha"` | The exact phrase, words next to each other |
| `"art"` | Only the whole word *art* |
| Chinese, Japanese or Korean text, e.g. `网球` | Exact substring search, the same as before. The word index can't split these languages into words |

- **Sort:** when you search, a menu next to the result count switches between **Newest** (the default) and **Best match**; the viewer remembers your last choice. Best match ranks sessions that use your words more often first, and counts a match in the title 5 times as much as one in the text. Newest groups results by day.
- **Previews:** the preview under each result shows the passage with your words highlighted.
- **Fallback:** if word search finds nothing, the old substring search runs instead.
- **Cap:** results stop at 300: the newest 300, or the best 300 under Best match.

The word index adds about 18 MB to `~/.cache/cc-sessions/index.db` (40 MB → 58 MB) and is built in under 1 s on first start.

## Favorites

Open a session and click **☆ Favorite** in its header to star it; starred sessions show ★ in the list. The **☆ Favorites** button under the search box shows only starred sessions; it combines with search and the folder filter, and the viewer remembers whether it is on. Stars are saved in `~/.claude/session-favorites.json`, outside the disposable index. Only the viewer page itself can change them: the server refuses writes from other sites.

## Move to trash

With `SV_TRASH` set (here `~/trash-box`, set in `~/.zshrc`), each session header has **Move to trash**. Click it, then click again within 4 seconds to confirm. The session's `.jsonl` and its subagent folder move to `~/trash-box/<date>-session-viewer/`, a row is added to `~/trash-box/_manifest.md`, and the session leaves the index, favorites and summaries. Sessions changed in the last 10 minutes are refused, since Claude Code may still be writing them.

*Written by Hai & Claude (claude-opus-5-5) | 2026-11-21 15:06 ET*
