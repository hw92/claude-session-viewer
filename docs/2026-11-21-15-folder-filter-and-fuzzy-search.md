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

*Written by Hai & Claude (claude-opus-5-5) | 2026-11-21 15:06 ET*
