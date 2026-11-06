# Custom Install

Use this guide to choose the install folder, add a short command, or change the resume button. For the quick install, see the [README](README.md).

There are two ways to use it:

- **Follow it yourself:** work through the steps below.
- **Let your Claude Code agent do it:** paste this into Claude Code on the computer you're installing on:

  > Install Claude Session Viewer by following https://github.com/hw92/claude-session-viewer/blob/main/CUSTOM-INSTALL.md. Ask me for the three choices first.

The steps are the same for a person or an agent.

## Choose three things

| Choice | Example | Default if you skip it |
|---|---|---|
| Install folder | `~/projects/tools/session-viewer` | `~/claude-session-viewer` |
| Short command | `sv` | No short command |
| Resume command | `claude --dangerously-skip-permissions`, or your own alias | `claude` |

The examples below use the folder and short command from this table. Replace them with your own choices.

The resume command is what the "Copy resume command" button uses. It runs in your terminal when you paste it, so an alias works if your shell has it.

## Mac or Linux

**1. Clone the repo**

```
mkdir -p ~/projects/tools
git clone https://github.com/hw92/claude-session-viewer ~/projects/tools/session-viewer
```

**2. Find your shell's config file**

Run `echo $SHELL`. Use `~/.zshrc` for zsh or `~/.bashrc` for bash.

**3. Check that the short command is free**

Open a new terminal and run:

```
type sv
```

If it prints "not found", the name is free. Otherwise, the name is taken, so pick another one.

**4. Add the settings**

Add these lines to the end of your config file. Leave out the `export` line to keep `claude` as the resume command.

```
alias sv='python3 ~/projects/tools/session-viewer/session-viewer.py'
export SV_RESUME_CMD=claude
```

**5. Run it**

Open a new terminal and type `sv`. Your browser opens at http://127.0.0.1:8765/.

If the viewer was already running, run `sv --stop` first so it picks up the new settings.

## Windows (PowerShell)

These steps have not yet been tested on a Windows computer.

**1. Clone the repo**

```powershell
mkdir -Force $HOME\projects\tools
git clone https://github.com/hw92/claude-session-viewer $HOME\projects\tools\session-viewer
```

**2. Check that Python runs**

Run `py --version`. If `py` isn't found, try `python --version` and use `python` in step 5.

**3. Check that the short command is free**

Run `Get-Command sv`. PowerShell has a built-in `sv` shortcut for `Set-Variable`, which step 5 removes. Keep that step only if you choose `sv`. If any other name you choose already exists, pick a different one.

**4. Allow PowerShell to load your profile**

You only need to run this once:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

An agent should ask before running this, because it changes a security setting for your Windows account.

**5. Add the settings**

Leave out the `$env:` line to keep `claude` as the resume command.

```powershell
if (!(Test-Path $PROFILE)) { New-Item -Force $PROFILE }
Add-Content $PROFILE @'
Remove-Item Alias:sv -Force -ErrorAction SilentlyContinue
function sv { py $HOME\projects\tools\session-viewer\session-viewer.py @args }
$env:SV_RESUME_CMD = "claude"
'@
```

PowerShell aliases can't pass options like `--stop`, so this uses a function.

**6. Run it**

Open a new PowerShell window and type `sv`. Your browser opens at http://127.0.0.1:8765/.

If the viewer was already running, run `sv --stop` first so it picks up the new settings.

## Check that it works

**Mac or Linux**

```
curl -s http://127.0.0.1:8765/api/sessions | python3 -c "import json,sys; print(len(json.load(sys.stdin)['sessions']), 'sessions')"
```

**Windows**

```powershell
(Invoke-RestMethod http://127.0.0.1:8765/api/sessions).sessions.Count
```

The first index can take a few seconds to a few minutes, so run the check again if the number is still growing. The count is `0` until Claude Code has sessions with typed prompts on this computer.

## Notes for agents

- Ask for the three choices once, before changing anything.
- Add to the shell config or profile. Don't replace its contents.
- If the install folder or short command already exists, stop and ask.
- Run the check above and report the number of sessions, the install path and the files you changed.
- Session files and the index can contain private conversations. Read only what the check needs, and don't copy that data anywhere.

## Commands

| Command | What it does |
|---|---|
| `sv` | Starts the viewer if needed and opens your browser |
| `sv --no-open` | Starts the viewer without opening your browser |
| `sv --stop` | Stops the viewer |
| `sv --serve` | Runs the viewer in the foreground |

## Change the port

The viewer uses port 8765. To change it, edit `PORT` near the top of `session-viewer.py`, then stop and restart the viewer.

## Update

Run `git pull` in the install folder, then `sv --stop` and `sv`.

## Uninstall

1. Run `sv --stop`.
2. Delete the install folder.
3. Delete `~/.cache/cc-sessions`, which holds the search index.
4. Remove the lines you added to your shell config or PowerShell profile.

Your Claude Code sessions are not touched.
