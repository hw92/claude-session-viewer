#!/usr/bin/env python3
"""session-viewer (alias sv): browse every Claude Code session in a browser, full history included.

Reads ~/.claude/projects/*/*.jsonl (read-only). Compaction only changes what the
model sees; the original messages stay in the file, so this shows all of them.

    session-viewer            start the server in the background if needed, open the browser
    session-viewer --serve    run the server in the foreground
    session-viewer --stop     stop the background server
    session-viewer find "words" [--since 7d] [--folder X] [--role R] [--type T] [--limit N] [--json]
                              find sessions; ranks summaries (from summarize.py) and full text together
    session-viewer show <session id, id prefix or key> [--grep word] [--max N]
                              print one session as plain text: your messages and Claude's replies

Set SV_RESUME_CMD to change the command in the resume button (default: claude).
Set SV_TRASH to a folder to turn on the Move to trash button (off by default).

Index with searchable text: ~/.cache/cc-sessions/index.db (safe to delete, rebuilt on start).
Binds to 127.0.0.1 only. Python stdlib only.
"""
import base64, bisect, collections, json, os, re, shlex, shutil, signal, sqlite3, subprocess, sys, threading, time, urllib.error, urllib.request, webbrowser
from concurrent.futures import ProcessPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROJECTS = Path.home() / ".claude/projects"
CACHE = Path.home() / ".cache/cc-sessions"
DB = CACHE / "index.db"
PID = CACHE / "server.pid"
FAVS = Path.home() / ".claude/session-favorites.json"  # starred sessions; kept outside the disposable cache
fav_lock = threading.Lock()
# Delete button: off unless SV_TRASH names a folder. Deleting moves the session there, never removes it.
TRASH = Path(os.path.expanduser(os.environ["SV_TRASH"])) if os.environ.get("SV_TRASH") else None
SUMMARIES = Path.home() / ".claude/session-summaries.db"
ACTIVE_SECONDS = 600  # refuse to delete a session changed this recently; Claude Code may still be writing it
PORT = 8765
WINDOWS = os.name == "nt"
RESUME_CMD = os.environ.get("SV_RESUME_CMD", "claude")
RESCAN_SECONDS = 60
MAX_BLOCK = 20000  # chars of one tool input/output sent to the page
SKIP_LINE = (b'{"type":"api-request', b'{"type":"file-history', b'{"type":"atis', b'{"type":"mode"',
             b'{"type":"permission-mode"', b'{"type":"session-owner"', b'{"type":"cost-state"')
NOISE = ("<local-command", "<task-notification", "<bash-stdout", "<bash-stderr", "Caveat: The messages below",
         "[Request interrupted")
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
REMINDER = re.compile(r"<(system-reminder|ide_opened_file|ide_selection)>.*?</\1>|</?pasted_content[^>]*>", re.S)
progress = {"done": 0, "total": 0, "running": False}
WORD = re.compile(r"[a-z][a-z0-9_-]{2,30}")
vocab = {"words": None, "by": {}, "sorted": []}  # every word in the index, for typo matching


# ---------- transcript parsing ----------

def records(path):
    """Yield (line_number, record) for the lines this tool cares about."""
    with open(path, "rb") as f:
        for n, line in enumerate(f):
            if line.startswith(SKIP_LINE):
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                yield n, rec


def user_text(rec):
    """Text the person actually typed, or None if this record is not a human message."""
    if rec.get("isMeta") or rec.get("isCompactSummary") or rec.get("isSidechain"):
        return None
    origin = rec.get("origin")
    if origin and origin.get("kind") != "human":
        return None
    c = (rec.get("message") or {}).get("content")
    if isinstance(c, list):
        if any(b.get("type") == "tool_result" for b in c):
            return None
        c = "\n".join(b.get("text", "") for b in c if b.get("type") == "text")
    t = REMINDER.sub("", c or "").strip()
    m = re.search(r"<command-name>(.*?)</command-name>", t, re.S)
    if m:
        a = re.search(r"<command-args>(.*?)</command-args>", t, re.S)
        return f"{m.group(1)} {a.group(1) if a else ''}".strip()
    m = re.search(r"<bash-input>(.*?)</bash-input>", t, re.S)
    if m:
        return "! " + m.group(1)
    if t.startswith(NOISE) or t.startswith("<command-message>"):
        return None
    return t


def content_blocks(rec):
    c = (rec.get("message") or {}).get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return c if isinstance(c, list) else []


def scan_file(args):
    """Summarize one transcript for the index. Runs in a worker process."""
    path, mtime, size = args
    s = {"path": path, "mtime": mtime, "size": size, "custom": "", "ai": "", "cwd": "", "first": "", "last": "",
         "prompt": "", "users": 0, "replies": 0, "compacts": 0}
    text = []
    try:
        for _, r in records(path):
            t = r.get("type")
            if t == "custom-title":
                s["custom"] = r.get("customTitle") or s["custom"]
            elif t == "ai-title":
                s["ai"] = r.get("aiTitle") or s["ai"]
            elif t == "system" and r.get("subtype") == "compact_boundary":
                s["compacts"] += 1
            elif t in ("user", "assistant") and not r.get("isSidechain"):
                s["cwd"] = s["cwd"] or r.get("cwd") or ""
                if t == "user":
                    u = user_text(r)
                    if u is None:
                        continue
                    if not u and not any(b.get("type") == "image" for b in content_blocks(r)):
                        continue
                    s["users"] += 1
                    s["prompt"] = s["prompt"] or u[:200]
                    text.append(u)
                else:
                    said = [b.get("text", "") for b in content_blocks(r) if b.get("type") == "text"]
                    if said:
                        s["replies"] += 1
                        text.extend(said)
                ts = r.get("timestamp") or ""
                s["first"] = s["first"] or ts
                s["last"] = ts or s["last"]
    except OSError:
        pass
    s["text"] = "\n".join(text)
    return s


def load_session(path):
    """Everything the page renders for one session, in file order."""
    items, tools, seen = [], {}, set()
    for n, r in records(path):
        t = r.get("type")
        uuid = r.get("uuid")
        if t not in ("user", "assistant", "system") or r.get("isSidechain"):
            continue
        if uuid in seen and uuid:
            continue
        seen.add(uuid)
        ts = r.get("timestamp", "")
        if t == "system":
            if r.get("subtype") == "compact_boundary":
                meta = r.get("compactMetadata") or {}
                items.append({"k": "compact", "ts": ts, "tokens": meta.get("preTokens"),
                              "trigger": meta.get("trigger"), "summary": ""})
            elif r.get("subtype") == "away_summary":
                items.append({"k": "recap", "ts": ts, "text": r.get("content", "")})
        elif t == "assistant":
            for b in content_blocks(r):
                bt = b.get("type")
                if bt == "text" and b.get("text", "").strip():
                    items.append({"k": "assistant", "ts": ts, "text": b["text"]})
                elif bt == "thinking" and b.get("thinking", "").strip():
                    items.append({"k": "thinking", "ts": ts, "out": clip(b["thinking"])})
                elif bt == "tool_use":
                    item = tool_item(b, ts)
                    tools[b.get("id")] = item
                    items.append(item)
        else:
            if r.get("isCompactSummary"):
                summary = "\n".join(b.get("text", "") for b in content_blocks(r))
                for it in reversed(items):
                    if it["k"] == "compact":
                        it["summary"] = summary
                        break
                continue
            blocks = content_blocks(r)
            for b in blocks:
                if b.get("type") == "tool_result" and b.get("tool_use_id") in tools:
                    tools[b["tool_use_id"]]["out"] = clip(result_text(b.get("content")))
                    tools[b["tool_use_id"]]["err"] = bool(b.get("is_error"))
            text = user_text(r)
            if text is None:
                continue
            images = [[n, i] for i, b in enumerate(blocks) if b.get("type") == "image"]
            if text or images:
                items.append({"k": "user", "ts": ts, "text": text, "images": images})
    return items


def tool_item(b, ts):
    inp = b.get("input") or {}
    hint = ""
    for key in ("description", "command", "file_path", "path", "pattern", "query", "skill", "url", "prompt"):
        if inp.get(key):
            hint = str(inp[key]).split("\n")[0][:140]
            break
    body = inp["command"] if isinstance(inp.get("command"), str) else json.dumps(inp, indent=1, ensure_ascii=False)
    return {"k": "tool", "ts": ts, "name": b.get("name", "tool"), "hint": hint, "in": clip(body), "out": ""}


def result_text(c):
    if isinstance(c, list):
        return "\n".join(x.get("text", "[image]") if isinstance(x, dict) else str(x) for x in c)
    return c if isinstance(c, str) else ""


def clip(s):
    return s if len(s) <= MAX_BLOCK else s[:MAX_BLOCK] + f"\n\n[cut for display: {len(s):,} chars in the jsonl]"


# ---------- index ----------

def connect():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    CACHE.mkdir(parents=True, exist_ok=True)
    CACHE.chmod(0o700)
    con = connect()
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS sessions(
        key TEXT PRIMARY KEY, id TEXT, path TEXT, mtime REAL, size INTEGER, title TEXT, renamed INTEGER,
        cwd TEXT, first TEXT, last TEXT, prompt TEXT, users INTEGER, replies INTEGER, compacts INTEGER, text TEXT)""")
    # Word index over title and text for ranked search; it reads the text from the sessions table.
    new = not con.execute("SELECT 1 FROM sqlite_master WHERE name='fts'").fetchone()
    con.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(title, text, content='sessions', content_rowid='rowid',
            tokenize='unicode61 remove_diacritics 2');
        CREATE TRIGGER IF NOT EXISTS fts_add AFTER INSERT ON sessions BEGIN
            INSERT INTO fts(rowid, title, text) VALUES (new.rowid, new.title, new.text); END;
        CREATE TRIGGER IF NOT EXISTS fts_del AFTER DELETE ON sessions BEGIN
            INSERT INTO fts(fts, rowid, title, text) VALUES ('delete', old.rowid, old.title, old.text); END;""")
    if new:
        con.execute("INSERT INTO fts(fts) VALUES ('rebuild')")
    con.commit()
    con.close()


def scan_all():
    """Index new or changed files; drop deleted ones. Unchanged files cost one stat."""
    on_disk = {}
    for p in PROJECTS.glob("*/*.jsonl"):
        try:
            st = p.stat()
        except OSError:
            continue
        on_disk[f"{p.parent.name}/{p.stem}"] = (str(p), st.st_mtime, st.st_size)
    con = connect()
    known = {r["key"]: (r["mtime"], r["size"]) for r in con.execute("SELECT key, mtime, size FROM sessions")}
    gone = [(k,) for k in known if k not in on_disk]
    con.executemany("DELETE FROM sessions WHERE key=?", gone)
    todo = [v for k, v in on_disk.items() if known.get(k) != (v[1], v[2])]
    progress.update(done=0, total=len(todo), running=bool(todo))
    if todo:
        with ProcessPoolExecutor(max(2, (os.cpu_count() or 4) - 2)) as pool:
            for s in pool.map(scan_file, todo, chunksize=4):
                p = Path(s["path"])
                con.execute("DELETE FROM sessions WHERE key=?", (f"{p.parent.name}/{p.stem}",))
                con.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    f"{p.parent.name}/{p.stem}", p.stem, s["path"], s["mtime"], s["size"],
                    s["custom"] or s["ai"] or s["prompt"][:90] or "(untitled)", int(bool(s["custom"])),
                    s["cwd"] or p.parent.name, s["first"], s["last"], s["prompt"],
                    s["users"], s["replies"], s["compacts"], s["text"]))
                progress["done"] += 1
                if progress["done"] % 50 == 0:
                    con.commit()
    con.commit()
    con.close()
    progress["running"] = False
    return len(todo) + len(gone)


def scan_loop():
    while True:
        try:
            if scan_all() or vocab["words"] is None:
                build_vocab()
        except Exception as e:
            progress["running"] = False
            print("scan failed:", e, file=sys.stderr)
        time.sleep(RESCAN_SECONDS)


def build_vocab():
    con = connect()
    cnt = collections.Counter()
    for (t,) in con.execute("SELECT lower(title||' '||text) FROM sessions WHERE users>0"):
        cnt.update(WORD.findall(t))
    con.close()
    by = {}
    for w in cnt:
        by.setdefault(w[0], []).append(w)
    vocab.update(words=cnt, by=by, sorted=sorted(w for w, n in cnt.items() if n >= 3))


def osa(a, b, k):
    """Edit distance counting a swap of neighbors as one edit; returns k+1 once it passes k."""
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
            if prev2 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > k:
            return k + 1
        prev2, prev = prev, cur
    return prev[-1]


def fuzzy(term):
    """Up to 5 known words within 1-2 typos of term, or [] when term is already a common word or word start."""
    k = 2 if len(term) >= 8 else 1 if len(term) >= 5 else 0
    if not k or not WORD.fullmatch(term):
        return []
    if vocab["words"] is None:
        build_vocab()
    s = vocab["sorted"]
    i = bisect.bisect_left(s, term)
    if i < len(s) and s[i].startswith(term):
        return []
    words = vocab["words"]
    near = [(osa(term, w, k), -words[w], w) for w in vocab["by"].get(term[0], ())
            if w != term and words[w] >= 3 and abs(len(w) - len(term)) <= k]
    return [w for d, _, w in sorted(x for x in near if x[0] <= k)[:5]]


COLS = "key,id,path,title,renamed,cwd,first,last,prompt,users,replies,compacts,size"


CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uf900-\ufaff]")
QUERY = re.compile(r'"([^"]*)"|(\S+)')


def fts_query(q, near):
    """FTS5 MATCH string. A plain word matches words starting with it (sess -> session); "quoted" text matches
    exactly; a misspelled word is ORed with its closest real words."""
    parts = []
    for phrase, word in QUERY.findall(q.lower()):
        text = (phrase or word).rstrip("*")
        if not re.search(r"\w", text):
            continue
        quoted = '"' + text.replace('"', '""') + '"'
        if phrase:
            parts.append(quoted)
        elif word.endswith("*"):
            parts.append(quoted + "*")
        else:
            parts.append("(" + " OR ".join([quoted + "*", *('"' + v + '"' for v in near.get(text, []))]) + ")")
    return " AND ".join(parts)


def list_sessions(q, sort="new"):
    """Ranked word search, falling back to substring search for Chinese/Japanese/Korean or when words find nothing.
    A misspelled term also matches its closest real words."""
    con = connect()
    terms = [t.strip('"*') for t in q.lower().split() if t.strip('"*')]
    near = {t: v for t in terms if (v := fuzzy(t))}
    rows = []
    match = "" if CJK.search(q) else fts_query(q, near)
    if match:
        order = "bm25(fts, 5.0, 1.0)" if sort == "rank" else "s.last DESC"
        cols = ",".join("s." + c for c in COLS.split(","))
        rows = con.execute(
            f"SELECT {cols}, snippet(fts, -1, char(2), char(3), '...', 28) AS snippet FROM fts "
            f"JOIN sessions s ON s.rowid = fts.rowid WHERE fts MATCH ? AND s.users>0 ORDER BY {order} LIMIT 300",
            [match]).fetchall()
    if terms and not rows:
        groups = [[t, *near.get(t, [])] for t in terms]
        has = "instr(lower(title||' '||text),?)>0"
        where = " AND ".join("(" + " OR ".join([has] * len(g)) + ")" for g in groups)
        at = ",".join(["nullif(instr(lower(text),?),0)"] * len(groups[0]))
        rows = con.execute(
            f"SELECT {COLS}, substr(text,max(coalesce({at},1)-60,1),190) AS snippet FROM sessions "
            f"WHERE users>0 AND {where} ORDER BY last DESC LIMIT 300",
            [*groups[0], *(x for g in groups for x in g)]).fetchall()
    elif not terms:
        rows = con.execute(f"SELECT {COLS} FROM sessions WHERE users>0 ORDER BY last DESC").fetchall()
    con.close()
    return [dict(r) for r in rows], near


def lookup(key):
    con = connect()
    row = con.execute(f"SELECT {COLS} FROM sessions WHERE key=?", (key,)).fetchone()
    con.close()
    return dict(row) if row else None


def load_favs():
    try:
        return json.loads(FAVS.read_text())
    except (OSError, ValueError):
        return {}


def set_fav(key, on):
    """Star or unstar one session; returns the starred keys."""
    with fav_lock:
        favs = load_favs()
        if on:
            favs.setdefault(key, time.strftime("%Y-%m-%dT%H:%M:%S"))
        else:
            favs.pop(key, None)
        tmp = FAVS.with_suffix(".tmp")
        tmp.write_text(json.dumps(favs, indent=1))
        tmp.replace(FAVS)
        return favs


def trash_session(row):
    """Move a session's jsonl and its sidecar folder (subagents, tool results) into TRASH/<date>-session-viewer/,
    log it in TRASH/_manifest.md, and drop it from the index, favorites and summaries. Returns the new folder."""
    src = Path(row["path"])
    if time.time() - src.stat().st_mtime < ACTIVE_SECONDS:
        raise ValueError("This session changed in the last 10 minutes and may still be running. Close it first.")
    dest = TRASH / (time.strftime("%Y-%m-%d") + "-session-viewer")
    dest.mkdir(parents=True, exist_ok=True)
    name = f"{src.parent.name}__{src.stem}"
    shutil.move(str(src), str(dest / (name + ".jsonl")))
    side = src.with_suffix("")
    if side.is_dir():
        shutil.move(str(side), str(dest / name))
    title = row["title"].replace("|", "/").replace("\n", " ")[:80]
    with open(TRASH / "_manifest.md", "a") as f:
        f.write(f"| {dest.name}/{name}.jsonl (session \"{title}\") | {src} | deleted in session viewer |\n")
    con = connect()
    con.execute("DELETE FROM sessions WHERE key=?", (row["key"],))
    con.commit()
    con.close()
    set_fav(row["key"], False)
    if SUMMARIES.exists():
        con = sqlite3.connect(SUMMARIES, timeout=30)
        con.execute("DELETE FROM summaries WHERE key=?", (row["key"],))
        con.commit()
        con.close()
    return str(dest).replace(str(Path.home()), "~", 1)


def resume_line(cwd, sid):
    if WINDOWS:  # PowerShell, the Windows Terminal default
        return f"cd '{cwd.replace(chr(39), chr(39) * 2)}'; {RESUME_CMD} --resume {sid}"
    return f"cd {shlex.quote(cwd)} && {RESUME_CMD} --resume {sid}"


# ---------- server ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/json; charset=utf-8", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def local(self):
        # Reject other hostnames so a web page cannot read this through DNS rebinding.
        return self.headers.get("Host") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def do_POST(self):
        # Writes must come from this page: same host, same origin, JSON body (other sites cannot send that without a preflight).
        if not self.local() or self.headers.get("Origin") not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}") \
                or not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self.send(b"forbidden", "text/plain", 403)
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 10000)) or b"{}")
            path, row = urlparse(self.path).path, lookup(str(body.get("key", "")))
            if path == "/api/fav" and row:
                return self.json({"favs": set_fav(body["key"], bool(body.get("on")))})
            if path == "/api/delete" and row and TRASH:
                try:
                    return self.json({"moved_to": trash_session(row)})
                except (ValueError, OSError) as e:
                    return self.json({"error": str(e)})
            self.send(b"not found", "text/plain", 404)
        except (ValueError, AttributeError):
            self.send(b"bad request", "text/plain", 400)

    def do_GET(self):
        if not self.local():
            return self.send(b"forbidden", "text/plain", 403)
        url = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/":
                return self.send(PAGE.encode(), "text/html; charset=utf-8")
            if url.path == "/api/sessions":
                rows, near = list_sessions(qs.get("q", ""), qs.get("sort", "new"))
                return self.json({"sessions": rows, "fuzzy": near, "favs": load_favs(), "can_delete": bool(TRASH),
                                  "progress": progress})
            row = lookup(qs.get("key", ""))
            if not row:
                return self.send(b"not found", "text/plain", 404)
            if url.path == "/api/session":
                row["resume"] = resume_line(row["cwd"], row["id"])
                return self.json({"session": row, "items": load_session(row["path"])})
            if url.path == "/api/image":
                return self.image(row["path"], int(qs["line"]), int(qs["i"]))
            self.send(b"not found", "text/plain", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json(self, obj):
        self.send(json.dumps(obj, ensure_ascii=False).encode())

    def image(self, path, line, i):
        for n, r in records(path):
            if n == line:
                src = content_blocks(r)[i].get("source") or {}
                if src.get("type") == "base64" and src.get("media_type") in IMAGE_TYPES:
                    return self.send(base64.b64decode(src["data"]), src["media_type"])
                break
        self.send(b"not found", "text/plain", 404)


def serve():
    init_db()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    PID.write_text(str(os.getpid()))
    threading.Thread(target=scan_loop, daemon=True).start()
    server.serve_forever()


def running():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/none", timeout=1)
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False
    return True


# ---------- command line: find / show ----------

def local_time(ts):
    from datetime import datetime
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    except ValueError:
        return ts[:16]


def since_iso(v):
    """'7d', '2w', '3m' or 'YYYY-MM-DD' -> UTC ISO string to compare with timestamps."""
    from datetime import datetime, timedelta, timezone
    m = re.fullmatch(r"(\d+)([dwm])", v or "")
    if m:
        days = int(m.group(1)) * {"d": 1, "w": 7, "m": 30}[m.group(2)]
        return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")
    return v or ""


def summary_rows():
    if not SUMMARIES.exists():
        return {}
    con = sqlite3.connect(SUMMARIES, timeout=30)
    con.row_factory = sqlite3.Row
    rows = {r["key"]: dict(r) for r in con.execute("SELECT * FROM summaries")}
    con.close()
    return rows


def summary_ranking(q, sums, near):
    """Keys whose title, summary or keywords match ANY query word (or phrase), best first."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE VIRTUAL TABLE f USING fts5(key UNINDEXED, title, summary, keywords, "
                "tokenize='unicode61 remove_diacritics 2')")
    con.executemany("INSERT INTO f VALUES(?,?,?,?)",
                    [(k, r["title"], r["summary"], " ".join(json.loads(r["keywords"] or "[]"))) for k, r in sums.items()])
    parts = [p for p in fts_query(q, near).split(" AND ") if p]
    if not parts:
        return []
    return [r[0] for r in con.execute("SELECT key FROM f WHERE f MATCH ? ORDER BY bm25(f, 3.0, 2.0, 2.0) LIMIT 60",
                                      [" OR ".join(parts)])]


def find(q, since="", folder="", role="", kind="", limit=10):
    """Rank sessions by summary match and full-text match together (reciprocal rank fusion)."""
    init_db()
    scan_all()
    sums = summary_rows()
    score = {}
    if q.strip():
        rows, near = list_sessions(q, "rank")
        if not CJK.search(q):
            for i, k in enumerate(summary_ranking(q, sums, near)):
                score[k] = score.get(k, 0) + 1.5 / (60 + i)
        for i, r in enumerate(rows[:60]):
            score[r["key"]] = score.get(r["key"], 0) + 1 / (60 + i)
        keys = sorted(score, key=score.get, reverse=True)
    else:
        con = connect()
        keys = [r[0] for r in con.execute("SELECT key FROM sessions WHERE users>0 ORDER BY last DESC")]
        con.close()
    cut, out = since_iso(since), []
    for k in keys:
        row = lookup(k)
        if not row:
            continue
        sm = sums.get(k, {})
        if (cut and row["last"] < cut) or (folder and folder.lower() not in row["cwd"].lower()) \
                or (role and sm.get("role") != role) or (kind and sm.get("type") != kind):
            continue
        out.append({"id": row["id"], "key": k, "title": row["title"], "folder": row["cwd"],
                    "first": row["first"], "last": row["last"], "prompts": row["users"],
                    "role": sm.get("role", ""), "type": sm.get("type", ""), "summary": sm.get("summary", ""),
                    "keywords": json.loads(sm.get("keywords") or "[]"), "resume": resume_line(row["cwd"], row["id"]),
                    "jsonl": row["path"]})
        if len(out) >= limit:
            break
    return out


def resolve(ref):
    """Session row from a key, a full session id, or an id prefix of 6+ characters."""
    row = lookup(ref)
    if row or len(ref) < 6:
        return row
    con = connect()
    hits = con.execute("SELECT key FROM sessions WHERE id LIKE ? ORDER BY last DESC", (ref + "%",)).fetchall()
    con.close()
    return lookup(hits[0][0]) if len(hits) == 1 else None


def show(ref, grep="", max_chars=40000):
    init_db()
    scan_all()
    row = resolve(ref)
    if not row:
        return print(f"no session matches {ref!r}", file=sys.stderr)
    sm = summary_rows().get(row["key"], {})
    print(f"# {row['title']}\nfolder: {row['cwd']}\ntime: {local_time(row['first'])} to {local_time(row['last'])}"
          f"\nresume: {resume_line(row['cwd'], row['id'])}\njsonl: {row['path']}")
    if sm:
        print(f"summary: {sm['summary']}\nkeywords: {', '.join(json.loads(sm['keywords']))}")
    blocks = []
    for it in load_session(row["path"]):
        if it["k"] == "user" and it["text"]:
            blocks.append(f"[{local_time(it['ts'])}] USER: {it['text']}")
        elif it["k"] == "assistant":
            blocks.append(f"CLAUDE: {it['text']}")
        elif it["k"] == "compact":
            blocks.append(f"[{local_time(it['ts'])}] --- compacted here ---")
    if grep:
        g = grep.lower()
        blocks = [b for b in blocks if g in b.lower()]
        print(f"({len(blocks)} messages contain {grep!r})")
    text = "\n\n".join(blocks)
    if len(text) > max_chars:
        half = max_chars // 2
        text = text[:half] + f"\n\n[... {len(text) - max_chars:,} chars skipped; use --grep or --max ...]\n\n" + text[-half:]
    print("\n" + text)


def cli(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="session-viewer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("find", help="find sessions")
    f.add_argument("query", nargs="?", default="")
    f.add_argument("--since", default="", help="7d, 2w, 3m or YYYY-MM-DD")
    f.add_argument("--folder", default="", help="part of the folder path")
    f.add_argument("--role", default="")
    f.add_argument("--type", default="")
    f.add_argument("--limit", type=int, default=10)
    f.add_argument("--json", action="store_true")
    s = sub.add_parser("show", help="print one session as text")
    s.add_argument("ref", help="session id, id prefix (6+ chars) or key")
    s.add_argument("--grep", default="")
    s.add_argument("--max", type=int, default=40000)
    a = ap.parse_args(argv)
    if a.cmd == "show":
        return show(a.ref, a.grep, a.max)
    res = find(a.query, a.since, a.folder, a.role, a.type, a.limit)
    if a.json:
        return print(json.dumps(res, indent=1, ensure_ascii=False))
    if not res:
        return print("no sessions found")
    for i, r in enumerate(res, 1):
        tag = f"  [{r['role']}/{r['type']}]" if r["role"] else ""
        print(f"{i}. {local_time(r['last'])}  {r['folder']}{tag}\n   {r['title']}")
        if r["summary"]:
            print(f"   {r['summary']}\n   keywords: {', '.join(r['keywords'])}")
        print(f"   resume: {r['resume']}\n")


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("find", "show"):
        return cli(sys.argv[1:])
    url = f"http://127.0.0.1:{PORT}/"
    if "--serve" in sys.argv:
        return serve()
    if "--stop" in sys.argv:
        if PID.exists():
            try:
                os.kill(int(PID.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass
            PID.unlink()
        for _ in range(10):
            if not running():
                return print("stopped")
            time.sleep(0.2)
        return print(f"still running on port {PORT}; stop that process yourself", file=sys.stderr)
    if not running():
        CACHE.mkdir(parents=True, exist_ok=True)
        with open(CACHE / "server.log", "a") as log:
            detach = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS \
                else {"start_new_session": True}
            subprocess.Popen([sys.executable, os.path.realpath(__file__), "--serve"], stdout=log, stderr=log,
                             stdin=subprocess.DEVNULL, **detach)
        for _ in range(30):
            time.sleep(0.2)
            if running():
                break
    print(url)
    if "--no-open" not in sys.argv:
        webbrowser.open(url)


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claude Sessions</title>
<style>
:root{--bg:#f7f6f3;--panel:#efede8;--line:#dcd9d1;--ink:#24231f;--dim:#6f6c63;--accent:#a8522f;--you:#e6e9ef;--code:#e9e7e1;--hit:#f5d97a;--sel:#e2ded4}
@media (prefers-color-scheme:dark){:root{--bg:#1e2024;--panel:#25282d;--line:#383c43;--ink:#e4e3df;--dim:#9a9a94;--accent:#e08a62;--you:#2e3540;--code:#2c2f35;--hit:#7d6412;--sel:#32363d}}
*{box-sizing:border-box}html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--ink);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;display:grid;grid-template-columns:350px minmax(0,1fr) 250px;overflow:hidden}
@media (max-width:1150px){body{grid-template-columns:320px minmax(0,1fr)}#outline{display:none}}
aside{background:var(--panel);display:flex;flex-direction:column;min-height:0;height:100vh}
#left{border-right:1px solid var(--line)}#outline{border-left:1px solid var(--line);overflow-y:auto;padding:14px 0}
#controls{padding:12px;display:grid;gap:8px;border-bottom:1px solid var(--line)}
input,select{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);font:inherit}
input:focus,select:focus{outline:2px solid var(--accent);outline-offset:-1px}
#status{font-size:12px;color:var(--dim);flex:1}#srow{display:flex;gap:8px;align-items:flex-start}
#sort{width:auto;padding:1px 4px;font-size:12px}
#fbox{position:relative}#fq.set{border-color:var(--accent);font-weight:600}
#fmenu{position:absolute;left:0;right:0;top:calc(100% + 3px);max-height:65vh;overflow-y:auto;background:var(--bg);border:1px solid var(--line);border-radius:6px;z-index:5;box-shadow:0 6px 20px rgba(0,0,0,.18)}
.fi{display:flex;gap:8px;align-items:baseline;padding:5px 9px;cursor:pointer}.fi.on{background:var(--sel)}
.fi b{font-weight:600;white-space:nowrap}.fi small{color:var(--dim);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}.fi i{color:var(--dim);font-size:12px;font-style:normal}
#head .cwd{cursor:pointer;text-decoration:underline dotted}
#list{overflow-y:auto;flex:1}
.day{position:sticky;top:0;background:var(--panel);padding:10px 12px 4px;font-size:12px;font-weight:600;color:var(--dim);z-index:1}
.row{display:block;width:100%;text-align:left;border:0;background:none;color:inherit;font:inherit;padding:7px 12px;cursor:pointer;border-left:3px solid transparent}
.row:hover{background:var(--sel)}.row.on{background:var(--sel);border-left-color:var(--accent)}
.b.del{margin-left:auto}.b.del.armed{color:#fff;background:#c0392b;border-color:#c0392b}
.row .star{color:var(--accent);margin-right:4px}#favbtn.on{border-color:var(--accent);color:var(--accent);font-weight:600}
#srow .b{flex:none}.b.fav{color:var(--accent);border-color:var(--accent)}
.row b{display:block;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row small{display:block;color:var(--dim);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row .snip{white-space:normal;margin-top:2px;color:var(--ink);opacity:.8}
main{overflow-y:auto;height:100vh;min-width:0}
#head{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 28px;z-index:2}
#head h1{font-size:17px;margin:0 0 2px;line-height:1.3}#head p{margin:0;color:var(--dim);font-size:12.5px;overflow-wrap:anywhere}
#head .btns{margin-top:7px;display:flex;gap:6px;flex-wrap:wrap}
button.b{font:inherit;font-size:12px;padding:3px 9px;border:1px solid var(--line);border-radius:5px;background:var(--panel);color:var(--ink);cursor:pointer}button.b:hover{border-color:var(--accent)}
#body{max-width:860px;margin:0 auto;padding:18px 28px 90px}
.empty{color:var(--dim);padding:60px 28px;text-align:center}
.msg{margin:0 0 16px;overflow-wrap:anywhere}.msg time,.date{color:var(--dim);font-size:12px}
.you{background:var(--you);border-radius:8px;padding:10px 13px;margin-top:24px}.you .t{white-space:pre-wrap}.you img{display:block;max-width:100%;max-height:420px;margin-top:8px;border-radius:5px}
.msg h1,.msg h2,.msg h3,.msg h4{font-size:15px;margin:14px 0 5px}.msg h1,.msg h2{font-size:16px}.msg p{margin:0 0 9px}.msg ul,.msg ol{margin:0 0 9px;padding-left:22px}
.msg blockquote{margin:0 0 9px;padding-left:11px;border-left:3px solid var(--line);color:var(--dim)}.msg hr{border:0;border-top:1px solid var(--line)}
code,pre{font-family:ui-monospace,Menlo,monospace;font-size:12.5px}code{background:var(--code);padding:1px 4px;border-radius:3px}
pre{background:var(--code);padding:9px 11px;border-radius:6px;overflow-x:auto;margin:0 0 9px;white-space:pre-wrap;overflow-wrap:anywhere}pre code{background:none;padding:0}
.scroll{overflow-x:auto;margin-bottom:9px}table{border-collapse:collapse;font-size:13px}th,td{border:1px solid var(--line);padding:3px 8px;text-align:left;vertical-align:top}th{background:var(--panel)}
a{color:var(--accent)}mark{background:var(--hit);color:inherit;border-radius:2px}
details.tools{margin:0 0 12px;color:var(--dim);font-size:12.5px}details summary{cursor:pointer}
details.one{margin:5px 0 5px 14px}details.one summary{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}details.one b{color:var(--ink);font-weight:600}details.one.err b{color:#c0392b}
details.one label{display:block;margin:6px 0 2px;font-size:11px;font-weight:600}
.rule{margin:28px 0;padding:10px 13px;border:1px dashed var(--accent);border-radius:8px;font-size:13px}.rule strong{color:var(--accent)}.rule pre{margin-top:8px;font-family:inherit;font-size:13px;background:none;padding:0}
.recap{font-style:italic;color:var(--dim);font-size:13px;margin:0 0 16px}
.date{text-align:center;margin:26px 0 6px;font-weight:600}
#outline h2{font-size:12px;color:var(--dim);margin:0 12px 7px;font-weight:600}
#outline button{display:block;width:100%;border:0;background:none;color:inherit;font:inherit;font-size:12.5px;line-height:1.35;text-align:left;padding:4px 12px;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#outline button:hover{background:var(--sel)}#outline button.c{color:var(--accent);font-weight:600}
.flash{animation:f 1.4s}@keyframes f{0%{outline:2px solid var(--accent)}100%{outline:2px solid transparent}}
</style></head><body>
<aside id="left"><div id="controls">
<input id="q" type="search" placeholder="Search all sessions  ( / )" autocomplete="off">
<div id="fbox"><input id="fq" type="search" placeholder="Filter by folder  ( f )" autocomplete="off" spellcheck="false"><div id="fmenu" hidden></div></div><div id="srow"><button id="favbtn" class="b" title="Show only starred sessions">☆ Favorites</button><div id="status"></div><select id="sort" title="Order of search results" hidden><option value="new">Newest</option><option value="rank">Best match</option></select></div></div><div id="list"></div></aside>
<main id="main"><div id="head" hidden></div><div id="body"><div class="empty">Pick a session on the left.</div></div></main>
<aside id="outline"></aside>
<script>
const $=id=>document.getElementById(id);
const fmt=(o,ts)=>ts?new Intl.DateTimeFormat('en-US',o).format(new Date(ts)):'';
const dayKey=ts=>fmt({year:'numeric',month:'2-digit',day:'2-digit'},ts);
const dayName=ts=>{const k=dayKey(ts),now=Date.now();if(k===dayKey(now))return'Today';if(k===dayKey(now-864e5))return'Yesterday';return fmt({weekday:'short',month:'short',day:'numeric',year:'numeric'},ts)};
const isoDay=ts=>ts?new Intl.DateTimeFormat('en-CA',{year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(ts)):'';
const clock=ts=>ts?fmt({hour:'numeric',minute:'2-digit'},ts)+' '+isoDay(ts):'';
const home=p=>(p||'').replace(/^(\/Users\/[^/]+|\/home\/[^/]+|[A-Za-z]:\\Users\\[^\\]+)/,'~');
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function inline(s){return s.split(/(`[^`\n]+`)/g).map(p=>{
  if(p.length>2&&p[0]==='`'&&p.endsWith('`'))return'<code>'+esc(p.slice(1,-1))+'</code>';
  return esc(p).replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>')
    .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g,'$1<em>$2</em>')
    .replace(/\[([^\]\n]+)\]\((https?:[^)\s]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>')}).join('')}
function md(src){const L=src.split('\n'),o=[];let i=0;
  const cells=l=>l.trim().replace(/^\||\|$/g,'').split('|').map(c=>c.trim());
  while(i<L.length){const l=L[i];let m;
    if(/^\s*```/.test(l)){const b=[];i++;while(i<L.length&&!/^\s*```/.test(L[i]))b.push(L[i++]);i++;o.push('<pre><code>'+esc(b.join('\n'))+'</code></pre>');continue}
    if(!l.trim()){i++;continue}
    if(m=l.match(/^(#{1,4})\s+(.*)/)){o.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`);i++;continue}
    if(/^\s*(---+|\*\*\*+)\s*$/.test(l)){o.push('<hr>');i++;continue}
    if(l.includes('|')&&i+1<L.length&&/^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(L[i+1])&&L[i+1].includes('-')){
      const head=cells(l);i+=2;let r='<div class="scroll"><table><tr>'+head.map(c=>'<th>'+inline(c)+'</th>').join('')+'</tr>';
      while(i<L.length&&L[i].includes('|')&&L[i].trim()){r+='<tr>'+cells(L[i]).map(c=>'<td>'+inline(c)+'</td>').join('')+'</tr>';i++}
      o.push(r+'</table></div>');continue}
    if(/^\s*([-*+]|\d+\.)\s+/.test(l)){const ol=/^\s*\d+\./.test(l),b=[];
      while(i<L.length&&/^\s*([-*+]|\d+\.)\s+/.test(L[i])){b.push('<li>'+inline(L[i].replace(/^\s*([-*+]|\d+\.)\s+/,''))+'</li>');i++}
      o.push((ol?'<ol>':'<ul>')+b.join('')+(ol?'</ol>':'</ul>'));continue}
    if(/^>\s?/.test(l)){const b=[];while(i<L.length&&/^>\s?/.test(L[i]))b.push(inline(L[i++].replace(/^>\s?/,'')));o.push('<blockquote>'+b.join('<br>')+'</blockquote>');continue}
    const b=[];while(i<L.length&&L[i].trim()&&!/^\s*```|^#{1,4}\s|^\s*([-*+]|\d+\.)\s+|^>\s?/.test(L[i]))b.push(inline(L[i++]));
    if(!b.length){b.push(inline(L[i++]))}
    o.push('<p>'+b.join('<br>')+'</p>')}
  return o.join('')}

let all=[],currentKey=null,query='',lastSig='',near={};
async function loadList(){
  const r=await(await fetch('/api/sessions?q='+encodeURIComponent(query)+'&sort='+$('sort').value)).json();$('sort').hidden=!query.trim();
  const sig=r.sessions.map(s=>s.key+s.last+s.title).join();
  if(sig!==lastSig){lastSig=sig;all=r.sessions;fillFolders();renderList()}
  near=r.fuzzy||{};canDelete=r.can_delete;if(JSON.stringify(r.favs)!==JSON.stringify(favs)){favs=r.favs||{};renderList();starBtn()}const p=r.progress;$('status').textContent=(p.running?`Indexing ${p.done} of ${p.total} changed files. `:'')+counts();
  clearTimeout(loadList.t);loadList.t=setTimeout(loadList,p.running?2000:30000)}
let folder='',folders=[],fpick=[],fsel=0;
let favs={},favOnly=false,canDelete=false;try{favOnly=localStorage.getItem('favOnly')==='1'}catch(e){}
function visible(){return all.filter(s=>(!folder||s.cwd===folder)&&(!favOnly||favs[s.key]))}
function setFavOnly(on){favOnly=on;try{localStorage.setItem('favOnly',on?'1':'0')}catch(e){}renderList();$('status').textContent=counts();$('list').scrollTop=0}
function favLabel(){const n=Object.keys(favs).length;$('favbtn').textContent=(favOnly?'★':'☆')+' Favorites'+(n?` (${n})`:'');$('favbtn').classList.toggle('on',favOnly)}
async function toggleFav(key){const r=await fetch('/api/fav',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key,on:!favs[key]})});
  if(!r.ok)return;favs=(await r.json()).favs;renderList();$('status').textContent=counts();starBtn()}
// Two clicks: the first arms the button for 4 seconds, the second moves the session to the trash folder.
function deleteBtn(key){const b=document.createElement('button');b.className='b del';b.textContent='Move to trash';
  b.onclick=async()=>{if(!b.armed){b.armed=1;b.textContent='Click again to confirm';b.classList.add('armed');
      setTimeout(()=>{b.armed=0;b.textContent='Move to trash';b.classList.remove('armed')},4000);return}
    b.disabled=true;const r=await(await fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key})})).json();
    if(r.error){b.textContent=r.error;return}
    all=all.filter(x=>x.key!==key);delete favs[key];currentKey=null;history.replaceState(null,'',location.pathname);renderList();$('status').textContent=counts();
    $('head').hidden=true;$('outline').innerHTML='';$('body').innerHTML=`<div class="empty">Moved to ${esc(r.moved_to)}. Move it back to ~/.claude/projects to restore it.</div>`};return b}
function starBtn(){const b=$('head').querySelector('.star-btn');if(!b)return;const on=!!favs[currentKey];b.textContent=on?'★ Favorited':'☆ Favorite';b.classList.toggle('fav',on)}
function counts(){const v=visible(),n=v.length,f=Object.entries(near).map(([t,w])=>`${t} → ${w.join(', ')}`).join('; ');
  return `${n}${query&&n===300?'+':''} session${n===1?'':'s'}${query?' match':''}${f?'. Also matching '+f:''}`}
function fillFolders(){const c={};for(const s of all){const f=c[s.cwd]||(c[s.cwd]={cwd:s.cwd,n:0,last:''});f.n++;if(s.last>f.last)f.last=s.last}
  folders=Object.values(c).sort((a,b)=>a.last<b.last?1:-1);if(!$('fmenu').hidden)showFolders()}
// Folder picker: every typed word must appear in the path; most recently used first.
function showFolders(){const typed=$('fq').classList.contains('set')?'':$('fq').value.toLowerCase(),terms=typed.split(/\s+/).filter(Boolean);
  fpick=folders.filter(f=>{const p=home(f.cwd).toLowerCase();return terms.every(t=>p.includes(t))}).slice(0,80);
  if(terms.length){const t=terms[terms.length-1];fpick.sort((a,b)=>(base(b.cwd).toLowerCase().includes(t))-(base(a.cwd).toLowerCase().includes(t)))}
  fsel=Math.min(fsel,Math.max(fpick.length-1,0));
  $('fmenu').innerHTML=(terms.length?'':`<div class="fi${fsel===-1?' on':''}" data-i="-1"><b>All folders</b><small></small><i>${all.length}</i></div>`)+
    fpick.map((f,i)=>`<div class="fi${i===fsel?' on':''}" data-i="${i}"><b>${esc(base(f.cwd))}</b><small>${esc(home(f.cwd).slice(0,-base(f.cwd).length).replace(/[\\/]$/,''))}</small><i>${f.n}</i></div>`).join('')||'<div class="fi"><small>No folder matches.</small></div>';
  $('fmenu').hidden=false;$('fmenu').querySelector('.fi.on')?.scrollIntoView({block:'nearest'})}
const base=p=>(p||'').split(/[\\/]/).filter(Boolean).pop()||p;
function setFolder(cwd){folder=cwd||'';const q=$('fq');q.value=folder?home(folder):'';q.classList.toggle('set',!!folder);q.title=folder;
  $('fmenu').hidden=true;renderList();$('status').textContent=counts();$('list').scrollTop=0}
function renderList(){let h='',day='';favLabel();const ranked=query.trim()&&$('sort').value==='rank';
  for(const s of visible()){const d=dayName(s.last);if(!ranked&&d!==day){day=d;h+=`<div class="day">${esc(d)}</div>`}
    const c=s.compacts?` · compacted ${s.compacts}x`:'';
    h+=`<button class="row${s.key===currentKey?' on':''}" data-k="${esc(s.key)}"><b>${favs[s.key]?'<span class="star">★</span>':''}${esc(s.title)}</b><small>${esc(clock(s.last))} · ${esc(home(s.cwd))} · ${s.users} prompts${c}</small>${s.snippet?`<small class="snip">${esc(s.snippet.replace(/\s+/g,' ')).replace(/\x02/g,'<mark>').replace(/\x03/g,'</mark>')}</small>`:''}</button>`}
  $('list').innerHTML=h||`<div class="empty">${favOnly&&!Object.keys(favs).length?'No favorites yet. Open a session and click ☆ Favorite.':'No sessions.'}</div>`;
  $('list').querySelectorAll('.row').forEach(b=>b.onclick=()=>{if(b.dataset.k===currentKey)openSession(currentKey);else location.hash=encodeURIComponent(b.dataset.k)})}

function copyBtn(label,value){const b=document.createElement('button');b.className='b';b.textContent=label;
  b.onclick=async()=>{await navigator.clipboard.writeText(value);b.textContent='Copied';setTimeout(()=>b.textContent=label,1200)};return b}
async function openSession(key){currentKey=key;renderList();
  $('head').hidden=true;$('outline').innerHTML='';$('body').innerHTML='<div class="empty">Loading the full transcript...</div>';
  const res=await fetch('/api/session?key='+encodeURIComponent(key));
  if(key!==currentKey)return;
  if(!res.ok){$('body').innerHTML='<div class="empty">This session is no longer on disk.</div>';return}
  const {session:s,items}=await res.json();
  if(key!==currentKey)return;
  const zone=new Intl.DateTimeFormat('en-US',{timeZoneName:'short'}).formatToParts(new Date(s.last)).find(x=>x.type==='timeZoneName').value;
  const span=`${clock(s.first)} to ${clock(s.last)} ${zone}`;
  $('head').innerHTML=`<h1>${esc(s.title)}</h1><p><span class="cwd" title="Show only this folder">${esc(home(s.cwd))}</span> · ${esc(span)} · ${s.users} prompts · ${s.compacts} compactions · ${(s.size/1048576).toFixed(1)} MB</p><div class="btns"></div>`;
  const star=document.createElement('button');star.className='b star-btn';star.onclick=()=>toggleFav(key);
  $('head').querySelector('.btns').append(star,copyBtn('Copy resume command',s.resume),copyBtn('Copy jsonl path',s.path));starBtn();
  if(canDelete)$('head').querySelector('.btns').append(deleteBtn(key));
  $('head').querySelector('.cwd').onclick=()=>setFolder(s.cwd);
  $('head').hidden=false;renderBody(s,items);$('main').scrollTop=0;jumpToHit()}
function renderBody(s,items){const body=$('body'),out=[];body.innerHTML='';let day='',group=null,n=0;
  const add=(el,ts)=>{group=null;const d=ts&&dayKey(ts);if(d&&d!==day){day=d;const x=document.createElement('div');x.className='date';x.textContent=dayName(ts);body.append(x)}body.append(el);return el};
  for(const it of items){
    if(it.k==='tool'||it.k==='thinking'){
      if(!group){group=document.createElement('details');group.className='tools';group.items=[];group.append(document.createElement('summary'));body.append(group);
        group.addEventListener('toggle',function(){if(this.open&&!this.done){this.done=1;this.items.forEach(t=>this.append(toolEl(t)))}})}
      group.items.push(it);const names=[...new Set(group.items.map(t=>t.k==='tool'?t.name:'thinking'))];
      group.firstChild.textContent=`${group.items.length} step${group.items.length>1?'s':''}: ${names.join(', ')}`;continue}
    const el=document.createElement('div');
    if(it.k==='user'){el.className='msg you';el.id='m'+n;
      el.innerHTML=`<time>${esc(clock(it.ts))}</time><div class="t">${esc(it.text)}</div>`+it.images.map(([l,i])=>`<img loading="lazy" alt="Attached image" src="/api/image?key=${encodeURIComponent(s.key)}&line=${l}&i=${i}">`).join('');
      out.push({id:el.id,label:it.text||'(image)'});n++}
    else if(it.k==='assistant'){el.className='msg';el.innerHTML=md(it.text)}
    else if(it.k==='recap'){el.className='recap';el.textContent='Recap: '+it.text}
    else if(it.k==='compact'){el.className='rule';el.id='m'+n;n++;
      const t=it.tokens?` at ${Math.round(it.tokens/1000)}k tokens`:'';
      el.innerHTML=`<strong>Compacted here</strong> · ${esc(clock(it.ts))}${esc(t)}. Claude only remembers the summary after this point; the messages above are the originals.`+(it.summary?`<details><summary>Summary Claude was given</summary><pre>${esc(it.summary)}</pre></details>`:'');
      out.push({id:el.id,label:'Compacted',c:1})}
    add(el,it.ts)}
  const o=$('outline');o.innerHTML=`<h2>${out.filter(x=>!x.c).length} prompts</h2>`;
  for(const x of out){const b=document.createElement('button');b.textContent=x.label.replace(/\s+/g,' ');b.title=x.label.slice(0,400);if(x.c)b.className='c';
    b.onclick=()=>{const t=$(x.id);t.scrollIntoView({block:'center'});t.classList.remove('flash');void t.offsetWidth;t.classList.add('flash')};o.append(b)}}
function toolEl(t){const d=document.createElement('details');d.className='one'+(t.err?' err':'');
  d.innerHTML=t.k==='thinking'?'<summary><b>Thinking</b></summary>':`<summary><b>${esc(t.name)}</b> ${esc(t.hint)}</summary>`;
  d.addEventListener('toggle',function(){if(!this.open||this.done)return;this.done=1;
    this.insertAdjacentHTML('beforeend',t.k==='thinking'?`<pre>${esc(t.out)}</pre>`:`<label>Input</label><pre>${esc(t.in)}</pre><label>Output</label><pre>${esc(t.out||'(none)')}</pre>`)});return d}
function jumpToHit(){const terms=query.replace(/["*]/g,' ').trim().split(/\s+/).filter(Boolean).flatMap(t=>[t,...(near[t.toLowerCase()]||[])]);if(!terms.length)return;
  const re=new RegExp(terms.map(t=>t.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('|'),'gi'),nodes=[];
  const w=document.createTreeWalker($('body'),NodeFilter.SHOW_TEXT);while(w.nextNode()){if(!w.currentNode.parentElement.closest('details')&&!w.currentNode.parentElement.closest('time'))nodes.push(w.currentNode)}
  let first=null;for(const n of nodes){const v=n.nodeValue;re.lastIndex=0;if(!re.test(v))continue;re.lastIndex=0;
    const f=document.createDocumentFragment();let a=0,m;while(m=re.exec(v)){f.append(v.slice(a,m.index));const k=document.createElement('mark');k.textContent=m[0];f.append(k);first=first||k;a=m.index+m[0].length}
    f.append(v.slice(a));n.replaceWith(f)}
  if(first)first.scrollIntoView({block:'center'})}

let timer;$('q').addEventListener('input',e=>{clearTimeout(timer);timer=setTimeout(()=>{query=e.target.value;lastSig='';loadList()},250)});
const fq=$('fq');
try{$('sort').value=localStorage.getItem('sort')||'new'}catch(e){}
$('favbtn').onclick=()=>setFavOnly(!favOnly);
$('sort').onchange=()=>{try{localStorage.setItem('sort',$('sort').value)}catch(e){}lastSig='';loadList()};
fq.addEventListener('focus',()=>{fsel=0;showFolders();if(fq.classList.contains('set'))fq.select()});
fq.addEventListener('input',()=>{fq.classList.remove('set');fsel=0;if(!fq.value&&folder){folder='';renderList();$('status').textContent=counts()}showFolders()});
fq.addEventListener('blur',()=>setTimeout(()=>{$('fmenu').hidden=true;if(!fq.classList.contains('set'))fq.value=folder?home(folder):'';fq.classList.toggle('set',!!folder)},120));
fq.addEventListener('keydown',e=>{const top=$('fq').value.trim()?0:-1;
  if(e.key==='ArrowDown'){e.preventDefault();fsel=Math.min(fsel+1,fpick.length-1);showFolders()}
  else if(e.key==='ArrowUp'){e.preventDefault();fsel=Math.max(fsel-1,top);showFolders()}
  else if(e.key==='Enter'){e.preventDefault();setFolder(fsel===-1?'':fpick[fsel]?.cwd||folder);fq.blur()}
  else if(e.key==='Escape'){e.preventDefault();setFolder('');fq.blur()}});
$('fmenu').addEventListener('mousedown',e=>{const d=e.target.closest('.fi[data-i]');if(!d)return;e.preventDefault();const i=+d.dataset.i;setFolder(i<0?'':fpick[i].cwd);fq.blur()});
addEventListener('keydown',e=>{if(/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName))return;
  if(e.key==='/'){e.preventDefault();$('q').focus()}else if(e.key==='f'&&!e.metaKey&&!e.ctrlKey){e.preventDefault();fq.focus()}});
addEventListener('hashchange',route);
function route(){const k=decodeURIComponent(location.hash.slice(1));if(k&&k!==currentKey)openSession(k)}
loadList().then(route);
</script></body></html>'''

if __name__ == "__main__":
    main()
