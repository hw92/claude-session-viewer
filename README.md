# Claude Session Viewer

Browse and search every Claude Code session on your computer, in your browser. Includes messages from before compaction, which you can no longer scroll back to in the terminal.

- One Python file, no dependencies, no build step
- Read-only by default: it never changes your session files unless you turn on the trash button (below)
- Local-only: it listens on `127.0.0.1` and makes no network calls

## Requirements

Python 3.8 or newer, on a computer where you use Claude Code. It reads `~/.claude/projects`, so each computer shows only its own sessions.

## Run it

**Mac or Linux**

```
git clone https://github.com/hw92/claude-session-viewer ~/claude-session-viewer
python3 ~/claude-session-viewer/session-viewer.py
```

**Windows (PowerShell)**

```
git clone https://github.com/hw92/claude-session-viewer $HOME\claude-session-viewer
py $HOME\claude-session-viewer\session-viewer.py
```

Your browser opens at http://127.0.0.1:8765/. The viewer keeps running in the background. Run the same command to open it again, including after a reboot.

## Custom install

To choose the folder, add a short command like `sv`, or change the resume button, see [CUSTOM-INSTALL.md](CUSTOM-INSTALL.md). You can follow it yourself or ask your Claude Code agent to do it for you.

## Moving sessions to trash

Set `SV_TRASH` to a folder before starting the viewer, e.g. `export SV_TRASH=~/trash-box`. Each session then gets a **Move to trash** button (click twice to confirm). It moves the session file and its subagent folder into `<SV_TRASH>/<date>-session-viewer/`, adds a line to `<SV_TRASH>/_manifest.md`, and never deletes anything. It refuses sessions changed in the last 10 minutes. To restore one, move it back to `~/.claude/projects/<folder>/`.

## Privacy

Your conversations can contain anything you pasted into them, including passwords and API keys. The viewer copies your messages and Claude's replies into a search index at `~/.cache/cc-sessions/index.db`. Delete that file at any time; it rebuilds the next time the viewer starts.

## Limits

- Subagent transcripts are not shown, only the tool call and result in the main session.
- Each tool input and output is cut at 20,000 characters for display. Your messages and Claude's replies are not cut.
- Claude Code deletes old sessions after `cleanupPeriodDays` in its settings. To keep them, raise that value.
