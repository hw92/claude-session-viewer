#!/usr/bin/env python3
"""summarize: write a summary, keywords and tags for each Claude Code session into a database.

    summarize.py --limit 20        summarize up to 20 sessions that need it (newest first)
    summarize.py --key <key>       summarize one session (key = <project-folder>/<session-id>)
    summarize.py --show 20         print the newest 20 summaries

Calls `claude -p --model haiku` (your Claude Code plan, no API key). Reads session text through
session-viewer.py's parser: your messages and Claude's replies, no tool output.
Database: ~/.claude/session-summaries.db. Not a cache: summaries cost usage to make, so keep it.
A session is (re)summarized when it is new, or changed and idle for an hour.

Roles and types are tags the model picks from fixed lists. Override them in ~/.claude/session-summaries.json:
    {"roles": {"work": "what counts as work", ...},          # tag name -> meaning, shown to the model
     "types": {"build": "...", ...},                         # optional, same shape
     "folder_roles": {"~/work": "work", ...}}                # fixed role per folder; longest prefix wins
"""
import argparse, importlib.util, json, sqlite3, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = Path.home() / ".claude/session-summaries.db"
MODEL = "haiku"
WORKERS = 4
IDLE_SECONDS = 3600           # wait this long after the last change before summarizing a session
HEAD, TAIL = 30000, 30000     # chars kept from the start and end of a long session
CONFIG = Path.home() / ".claude/session-summaries.json"   # your own tags; kept out of this repo
DEFAULT = {
    "roles": {"work": "job or business tasks", "personal": "personal life, health, family, money, chores",
              "learning": "studying a subject for its own sake", "mixed": "spans several of these"},
    "types": {"build": "making or changing software or files", "debug": "fixing something broken",
              "research": "finding things out", "writing": "documents, posts, emails",
              "admin": "setup, config, accounts, chores", "learning": "understanding a concept or tool"},
    "folder_roles": {}}
try:
    CFG = {**DEFAULT, **json.loads(CONFIG.read_text())}
except FileNotFoundError:
    CFG = DEFAULT
ROLES, TYPES, FOLDER_ROLES = list(CFG["roles"]), list(CFG["types"]), CFG["folder_roles"]

spec = importlib.util.spec_from_file_location("sv", HERE / "session-viewer.py")
sv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sv)

SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["summary", "keywords", "role", "type"],
          "properties": {
              "summary": {"type": "string"},
              "keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
              "role": {"type": "string", "enum": ROLES},
              "type": {"type": "string", "enum": TYPES}}}

PROMPT = f"""You index past Claude Code sessions so the user can find them later by meaning.
Read the session transcript and return JSON:
- summary: 2-4 plain sentences. What the user wanted, what was done, how it ended (done, open, abandoned).
  Name concrete things: projects, files, tools, services, tickers, people's roles. No filler.
  Start with the verb and leave out the user ("Tailored a resume...", not "He tailored...").
- keywords: 3-8 short lowercase terms someone would search for (project names, tools, topics, tickers).
- role: the user's role in this session, one of:
{chr(10).join(f"  {k} = {v}" for k, v in CFG["roles"].items())}
- type: the kind of work, one of:
{chr(10).join(f"  {k} = {v}" for k, v in CFG["types"].items())}"""


def folder_role(cwd):
    p = cwd.replace(str(Path.home()), "~", 1)
    hits = [k for k in FOLDER_ROLES if p == k or p.startswith(k + "/")]
    return FOLDER_ROLES[max(hits, key=len)] if hits else None


def connect():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS summaries(
        key TEXT PRIMARY KEY, id TEXT, cwd TEXT, title TEXT, summary TEXT, keywords TEXT, role TEXT, type TEXT,
        model TEXT, made_at TEXT, source_mtime REAL, source_size INTEGER, source_last TEXT)""")
    return con


def transcript(path):
    """Plain text of the session: user prompts, Claude's text replies, compaction summaries."""
    out = []
    for it in sv.load_session(path):
        if it["k"] == "user" and it["text"]:
            out.append("USER: " + it["text"])
        elif it["k"] == "assistant":
            out.append("CLAUDE: " + it["text"])
        elif it["k"] == "compact" and it["summary"]:
            out.append("[COMPACTION SUMMARY]\n" + it["summary"])
    text = "\n\n".join(out)
    if len(text) > HEAD + TAIL:
        text = text[:HEAD] + f"\n\n[... {len(text) - HEAD - TAIL:,} chars skipped ...]\n\n" + text[-TAIL:]
    return text


def summarize(row):
    """Run haiku on one session; return the parsed JSON or raise."""
    body = (f"Folder: {row['cwd'].replace(str(Path.home()), '~', 1)}\n"
            f"Title: {row['title']}\nDates: {row['first'][:10]} to {row['last'][:10]}\n\n"
            f"<transcript>\n{transcript(row['path'])}\n</transcript>")
    cmd = ["claude", "-p", "--model", MODEL, "--output-format", "json", "--no-session-persistence",
           "--tools", "", "--strict-mcp-config", "--disable-slash-commands", "--setting-sources", "",
           "--system-prompt", PROMPT, "--json-schema", json.dumps(SCHEMA)]
    r = subprocess.run(cmd, input=body, capture_output=True, text=True, timeout=300, cwd=str(Path.home()))
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[-300:])
    res = json.loads(r.stdout)
    if res.get("is_error"):
        raise RuntimeError(str(res.get("result"))[-300:])
    data = res.get("structured_output") or json.loads(res["result"])
    return data, res.get("modelUsage") and next(iter(res["modelUsage"])) or MODEL


def todo(con, limit, key=None):
    """Sessions from the viewer's index that are new or changed-and-idle, newest first."""
    idx = sv.connect()
    if key:
        rows = idx.execute(f"SELECT {sv.COLS}, mtime FROM sessions WHERE key=?", (key,)).fetchall()
    else:
        done = {r["key"]: (r["source_mtime"], r["source_size"]) for r in con.execute(
            "SELECT key, source_mtime, source_size FROM summaries")}
        now = time.time()
        rows = [r for r in idx.execute(f"SELECT {sv.COLS}, mtime FROM sessions WHERE users>0 ORDER BY last DESC")
                if done.get(r["key"]) != (r["mtime"], r["size"]) and now - r["mtime"] > IDLE_SECONDS][:limit]
    idx.close()
    return rows


def run(rows, con):
    ok = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(WORKERS) as pool:
        futs = {pool.submit(summarize, dict(r)): r for r in rows}
        for f in futs:
            r = futs[f]
            try:
                data, model = f.result()
                data["role"] = folder_role(r["cwd"]) or data["role"]
            except Exception as e:
                fail += 1
                print(f"FAIL {r['key']}: {e}", file=sys.stderr)
                continue
            con.execute("INSERT OR REPLACE INTO summaries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                r["key"], r["id"], r["cwd"], r["title"], data["summary"].strip(),
                json.dumps([k.lower() for k in data["keywords"]], ensure_ascii=False), data["role"], data["type"],
                model, datetime.now(timezone.utc).isoformat(timespec="seconds"), r["mtime"], r["size"], r["last"]))
            con.commit()
            ok += 1
            print(f"[{ok + fail}/{len(rows)}] {data['role']:10} {data['type']:8} {r['title'][:60]}")
    print(f"done: {ok} summarized, {fail} failed, {time.time() - t0:.0f}s")


def show(con, n):
    for r in con.execute("SELECT * FROM summaries ORDER BY source_last DESC LIMIT ?", (n,)):
        print(f"\n## {r['title']}\n{r['source_last'][:10]} | {r['cwd']} | {r['role']} / {r['type']}\n"
              f"{r['summary']}\nkeywords: {', '.join(json.loads(r['keywords']))}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--key")
    ap.add_argument("--show", type=int)
    a = ap.parse_args()
    con = connect()
    if a.show:
        return show(con, a.show)
    for r in con.execute("SELECT key, cwd FROM summaries").fetchall():  # apply map changes to existing rows
        if folder_role(r["cwd"]):
            con.execute("UPDATE summaries SET role=? WHERE key=?", (folder_role(r["cwd"]), r["key"]))
    con.commit()
    sv.init_db()
    rows = todo(con, a.limit, a.key)
    print(f"{len(rows)} sessions to summarize with {MODEL}, {WORKERS} at a time")
    run(rows, con)


if __name__ == "__main__":
    main()
