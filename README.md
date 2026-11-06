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

## Optional: a short command (Mac or Linux)

```
mkdir -p ~/bin
ln -s ~/claude-session-viewer/session-viewer.py ~/bin/session-viewer
echo 'alias sv=session-viewer' >> ~/.zshrc
```

`~/bin` must be on your `PATH`. Then type `sv` in a new terminal.

## Options

| Command or setting | What it does |
|---|---|
| `--stop` | Stops the background viewer |
| `--no-open` | Starts the viewer without opening a browser |
| `--serve` | Runs the viewer in the foreground |
| `SV_RESUME_CMD` | Command used by the "Copy resume command" button. Default: `claude` |

The Windows resume command is written for PowerShell.

## Privacy

Your conversations can contain anything you pasted into them, including passwords and API keys. The viewer copies your messages and Claude's replies into a search index at `~/.cache/cc-sessions/index.db`. Delete that file at any time; it rebuilds the next time the viewer starts.

## Limits

- Subagent transcripts are not shown, only the tool call and result in the main session.
- Each tool input and output is cut at 20,000 characters for display. Your messages and Claude's replies are not cut.
- Claude Code deletes old sessions after `cleanupPeriodDays` in its settings. To keep them, raise that value.
