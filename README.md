# Claude Session Viewer

Browse and search every Claude Code session on your computer, in your browser. Includes messages from before compaction, which you can no longer scroll back to in the terminal.

- One Python file, no dependencies, no build step
- Read-only: it never changes your session files
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

## Privacy

Your conversations can contain anything you pasted into them, including passwords and API keys. The viewer copies your messages and Claude's replies into a search index at `~/.cache/cc-sessions/index.db`. Delete that file at any time; it rebuilds the next time the viewer starts.

## Limits

- Subagent transcripts are not shown, only the tool call and result in the main session.
- Each tool input and output is cut at 20,000 characters for display. Your messages and Claude's replies are not cut.
- Claude Code deletes old sessions after `cleanupPeriodDays` in its settings. To keep them, raise that value.
