---
name: find-session
description: Find a past Claude Code session from a vague memory of what was being done, and give back its folder and resume command. Use when the user asks "which session did I...", "what was I doing with X", "find the session where...", "where did I work on...", "resume the session about...", "what did I do last week/yesterday", "上次那个...在哪个 session", or wants to continue earlier work but has lost the session id, the folder, or the exact words. Searches all projects on this machine, not just the current one.
---

# find-session

Finds past Claude Code sessions on this machine and hands back what is needed to resume one: folder, session id, resume command.

The tool is `session-viewer` (on PATH; otherwise `python3 <repo>/session-viewer.py`, the claude-session-viewer repo). It reads `~/.claude/projects/*.jsonl` and, when `summarize.py` has run, a summary, keywords, role and type for each session.

## Steps

1. **Turn the memory into search words.** `find` matches words, not meaning, so do the meaning part yourself. Pick concrete nouns the session would contain: project names, tools, services, tickers, file names, people's roles. For a vague ask, plan 2-3 wordings (synonyms, the likely project name, the tool used).
   - "the time I set up keys so agents stop asking me" → `agent keys credentials`, then `api key env skill`
2. **Search.** Run each wording, all in one turn:
   ```bash
   session-viewer find "agent keys credentials" --limit 8
   ```
   Filters: `--since 7d|2w|3m|YYYY-MM-DD`, `--folder <part of path>`, `--role <role tag>`, `--type build|debug|research|writing|admin|learning`, `--json`.
   For "what did I do yesterday/last week", leave out the words: `session-viewer find --since 1d --limit 20`.
3. **Pick.** Read the summaries and choose the session(s) that fit what the user described. If none fit, try other words once more before saying it was not found.
4. **Drill in only when needed.** If the summary does not answer the question, read the session itself:
   ```bash
   session-viewer show <session id or 8-char prefix> --grep "<word>"
   ```
   `show` prints messages only, without tool output. Never grep or cat the raw `.jsonl`; they can be hundreds of MB.
5. **Answer.** Lead with the session that fits, then:
   - date, folder, one line on what was done and how it ended
   - the resume command in its own code block, copied exactly from the output:
     ```
     cd /path/to/folder && claude --resume <session id>
     ```
   - other candidates as one line each (date, folder, title), if any are close.

## Notes

- Sessions without a summary still show up through full-text search; they just have no summary line.
- Summaries can lag up to an hour behind active sessions; the full-text search is always current.
- The browser view of everything is `session-viewer` with no arguments (http://127.0.0.1:8765/).
