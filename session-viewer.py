#!/usr/bin/env python3
"""session-viewer (alias sv): browse every Claude Code session in a browser, full history included.

Reads ~/.claude/projects/*/*.jsonl (read-only). Compaction only changes what the
model sees; the original messages stay in the file, so this shows all of them.

    session-viewer            start the server in the background if needed, open the browser
    session-viewer --serve    run the server in the foreground
    session-viewer --stop     stop the background server

Set SV_RESUME_CMD to change the command in the resume button (default: claude).

Index with searchable text: ~/.cache/cc-sessions/index.db (safe to delete, rebuilt on start).
Binds to 127.0.0.1 only. Python stdlib only.
"""
import base64, json, os, re, shlex, signal, sqlite3, subprocess, sys, threading, time, urllib.error, urllib.request, webbrowser
from concurrent.futures import ProcessPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROJECTS = Path.home() / ".claude/projects"
CACHE = Path.home() / ".cache/cc-sessions"
DB = CACHE / "index.db"
PID = CACHE / "server.pid"
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
                con.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
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


def scan_loop():
    while True:
        try:
            scan_all()
        except Exception as e:
            progress["running"] = False
            print("scan failed:", e, file=sys.stderr)
        time.sleep(RESCAN_SECONDS)


COLS = "key,id,path,title,renamed,cwd,first,last,prompt,users,replies,compacts,size"


def list_sessions(q):
    con = connect()
    terms = q.lower().split()
    if terms:
        where = " AND ".join(["instr(lower(title||' '||text),?)>0"] * len(terms))
        rows = con.execute(
            f"SELECT {COLS}, substr(text,max(instr(lower(text),?)-60,1),190) AS snippet FROM sessions "
            f"WHERE users>0 AND {where} ORDER BY last DESC LIMIT 300", [terms[0], *terms]).fetchall()
    else:
        rows = con.execute(f"SELECT {COLS} FROM sessions WHERE users>0 ORDER BY last DESC").fetchall()
    con.close()
    return [dict(r) for r in rows]


def lookup(key):
    con = connect()
    row = con.execute(f"SELECT {COLS} FROM sessions WHERE key=?", (key,)).fetchone()
    con.close()
    return dict(row) if row else None


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

    def do_GET(self):
        # Reject other hostnames so a web page cannot read this through DNS rebinding.
        if self.headers.get("Host") not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            return self.send(b"forbidden", "text/plain", 403)
        url = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/":
                return self.send(PAGE.encode(), "text/html; charset=utf-8")
            if url.path == "/api/sessions":
                return self.json({"sessions": list_sessions(qs.get("q", "")), "progress": progress})
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


def main():
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
        return print("stopped")
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
#status{font-size:12px;color:var(--dim)}
#list{overflow-y:auto;flex:1}
.day{position:sticky;top:0;background:var(--panel);padding:10px 12px 4px;font-size:12px;font-weight:600;color:var(--dim);z-index:1}
.row{display:block;width:100%;text-align:left;border:0;background:none;color:inherit;font:inherit;padding:7px 12px;cursor:pointer;border-left:3px solid transparent}
.row:hover{background:var(--sel)}.row.on{background:var(--sel);border-left-color:var(--accent)}
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
<select id="folder"></select><div id="status"></div></div><div id="list"></div></aside>
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

let all=[],currentKey=null,query='',lastSig='';
async function loadList(){
  const r=await(await fetch('/api/sessions?q='+encodeURIComponent(query))).json();
  const sig=r.sessions.map(s=>s.key+s.last+s.title).join();
  if(sig!==lastSig){lastSig=sig;all=r.sessions;fillFolders();renderList()}
  const p=r.progress;$('status').textContent=(p.running?`Indexing ${p.done} of ${p.total} changed files. `:'')+counts();
  clearTimeout(loadList.t);loadList.t=setTimeout(loadList,p.running?2000:30000)}
function visible(){const f=$('folder').value;return f?all.filter(s=>s.cwd===f):all}
function counts(){const v=visible(),n=v.length;return `${n}${query&&n===300?'+':''} session${n===1?'':'s'}${query?' match':''}`}
function fillFolders(){const keep=$('folder').value,c={};all.forEach(s=>c[s.cwd]=(c[s.cwd]||0)+1);
  const names=Object.keys(c).sort((a,b)=>c[b]-c[a]);
  $('folder').innerHTML='<option value="">All folders</option>'+names.map(n=>`<option value="${esc(n)}">${esc(home(n))} (${c[n]})</option>`).join('');
  $('folder').value=names.includes(keep)?keep:''}
function renderList(){let h='',day='';
  for(const s of visible()){const d=dayName(s.last);if(d!==day){day=d;h+=`<div class="day">${esc(d)}</div>`}
    const c=s.compacts?` · compacted ${s.compacts}x`:'';
    h+=`<button class="row${s.key===currentKey?' on':''}" data-k="${esc(s.key)}"><b>${esc(s.title)}</b><small>${esc(clock(s.last))} · ${esc(home(s.cwd))} · ${s.users} prompts${c}</small>${s.snippet?`<small class="snip">${esc(s.snippet.replace(/\s+/g,' '))}</small>`:''}</button>`}
  $('list').innerHTML=h||'<div class="empty">No sessions.</div>';
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
  $('head').innerHTML=`<h1>${esc(s.title)}</h1><p>${esc(home(s.cwd))} · ${esc(span)} · ${s.users} prompts · ${s.compacts} compactions · ${(s.size/1048576).toFixed(1)} MB</p><div class="btns"></div>`;
  $('head').querySelector('.btns').append(copyBtn('Copy resume command',s.resume),copyBtn('Copy jsonl path',s.path));
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
function jumpToHit(){const terms=query.trim().split(/\s+/).filter(Boolean);if(!terms.length)return;
  const re=new RegExp(terms.map(t=>t.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('|'),'gi'),nodes=[];
  const w=document.createTreeWalker($('body'),NodeFilter.SHOW_TEXT);while(w.nextNode()){if(!w.currentNode.parentElement.closest('details')&&!w.currentNode.parentElement.closest('time'))nodes.push(w.currentNode)}
  let first=null;for(const n of nodes){const v=n.nodeValue;re.lastIndex=0;if(!re.test(v))continue;re.lastIndex=0;
    const f=document.createDocumentFragment();let a=0,m;while(m=re.exec(v)){f.append(v.slice(a,m.index));const k=document.createElement('mark');k.textContent=m[0];f.append(k);first=first||k;a=m.index+m[0].length}
    f.append(v.slice(a));n.replaceWith(f)}
  if(first)first.scrollIntoView({block:'center'})}

let timer;$('q').addEventListener('input',e=>{clearTimeout(timer);timer=setTimeout(()=>{query=e.target.value;lastSig='';loadList()},250)});
$('folder').onchange=()=>{renderList();$('status').textContent=counts()};
addEventListener('keydown',e=>{if(e.key==='/'&&!/INPUT|SELECT/.test(document.activeElement.tagName)){e.preventDefault();$('q').focus()}});
addEventListener('hashchange',route);
function route(){const k=decodeURIComponent(location.hash.slice(1));if(k&&k!==currentKey)openSession(k)}
loadList().then(route);
</script></body></html>'''

if __name__ == "__main__":
    main()
