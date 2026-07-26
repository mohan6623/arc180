#!/usr/bin/env python3
"""ARC/180 — local app server.

Serves the ARC/180 PWA and a small JSON API on top of the
a markdown knowledge base. No third-party dependencies.

  GET  /api/state[?user=<name>]   full app state (schedule, progress, notes, chats)
  POST /api/claim                 {user, date} mark a day done (app-side claim)
  GET  /api/note?user=&date=      raw note markdown
  POST /api/note                  {user, date, content} write note into the KB
  POST /api/chat                  {user, provider, message, convo_id?} ask the KB
  GET  /api/convo?id=             one conversation

Data ownership:
  - Schedule (template) and people/<user>/progress are READ ONLY here.
  - The app writes only people/<user>/notes/<date>.md and its own data/*.json.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DATA = ROOT / "data"

# Everything machine- or person-specific lives in arc180.local.json, which is
# git-ignored. Copy arc180.local.example.json and edit it. Nothing in this file
# should identify a person, a path, or a credential.
CONF_FILE = ROOT / "arc180.local.json"
_conf = {}
if CONF_FILE.exists():
    try:
        _conf = json.loads(CONF_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"warning: {CONF_FILE.name} is not valid JSON — using defaults")

KB = Path(os.environ.get("ARC180_KB") or _conf.get("kb_path") or (ROOT.parent / "knowledge-base"))
KB_NAME = KB.name
PORT = int(os.environ.get("ARC180_PORT") or _conf.get("port") or 8990)

# Exactly two learners duel. Names must match the people/<name>/ folders in the
# knowledge base and the `user:` value in its whoami file.
USERS = list(_conf.get("users") or ["learner-a", "learner-b"])[:2]
if len(USERS) < 2:
    USERS = (USERS + ["learner-a", "learner-b"])[:2]

SUPABASE_URL = _conf.get("supabase_url", "")
SUPABASE_KEY = _conf.get("supabase_anon_key", "")
CLOUD_ENABLED = bool(SUPABASE_URL and SUPABASE_KEY)
# Curriculum window. Day 1 of the plan and how many days it runs.
PLAN_START = date.fromisoformat(_conf.get("plan_start", "2026-07-01"))
PLAN_DAYS = int(_conf.get("plan_days", 180))

XP_WEEKDAY = 50
XP_BUILD = 100
RANKS = [(0, "Apprentice"), (300, "Draftsman"), (600, "Engineer"),
         (900, "Senior Engineer"), (1200, "Staff Engineer"),
         (1800, "Architect"), (2600, "Principal Architect")]

MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
          "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}

_lock = threading.Lock()


# ---------------------------------------------------------------- schedule --
def whoami():
    """The KB's own identity mechanism: whoami.local.md (git-ignored) says
    who is sitting at this machine, so the same code serves each learner
    their own side on their own PC."""
    f = KB / "whoami.local.md"
    if f.exists():
        m = re.search(r"^user:\s*(\w+)", f.read_text(encoding="utf-8", errors="replace"), re.M)
        if m and m.group(1) in USERS:
            return m.group(1)
    return USERS[0]


def parse_frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    fm = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition(":")
                fm[k.strip()] = v.strip().strip('"')
    return fm


def load_schedule():
    """date(iso) -> {title, task, week, week_title, month_title, kind}"""
    days = {}
    weeks = []
    for f in sorted(KB.glob("schedule/month-*/week-*.md")):
        text = f.read_text(encoding="utf-8", errors="replace")
        fm = parse_frontmatter(text)
        week_no = int(fm.get("week", 0) or 0)
        week_title = fm.get("title", f.stem)
        month_title = f.parent.name.replace("-", " ").replace("month ", "Month ").title()
        weeks.append({"week": week_no, "title": week_title,
                      "dates": fm.get("dates", ""), "month": month_title})
        # weekday sections: ### Mon Jul 6 — REST fundamentals
        for m in re.finditer(
                r"^### (\w{3}) (\w{3}) (\d{1,2}) — (.+?)\n(.*?)(?=^###|^## |\Z)",
                text, re.S | re.M):
            _dow, mon, dd, title, body = m.groups()
            if mon not in MONTHS:
                continue
            d = date(2026, MONTHS[mon], int(dd))
            task = ""
            bm = re.search(r"^- (.+?)(?:\n  |\n-|\n\n|\Z)", body.strip(), re.S)
            if bm:
                task = re.sub(r"\s+", " ", bm.group(1)).strip()
            days[d.isoformat()] = {
                "title": title.strip(), "task": task, "week": week_no,
                "week_title": week_title, "month_title": month_title,
                "kind": "build" if d.weekday() == 5 else "study",
            }
        # Saturday build section (## Saturday — Build day)
        sat = re.search(r"^## Saturday — (.+?)\n\n\*\*(.+?)\*\*\n\n- (.+?)(?:\n  |\n\n|\Z)",
                        text, re.S | re.M)
        if sat and fm.get("dates"):
            dm = re.search(r"Fri (\w{3}) (\d{1,2})", fm["dates"])
            if dm and dm.group(1) in MONTHS:
                d = date(2026, MONTHS[dm.group(1)], int(dm.group(2))) + timedelta(days=1)
                days[d.isoformat()] = {
                    "title": sat.group(2).strip(),
                    "task": re.sub(r"\s+", " ", sat.group(3)).strip(),
                    "week": week_no, "week_title": week_title,
                    "month_title": month_title, "kind": "build",
                }
    return days, sorted(weeks, key=lambda w: w["week"])


# ---------------------------------------------------------------- progress --
def load_progress(user):
    """From KB progress files: date(iso) -> {status, title, subtasks:[{text,done}]}"""
    out = {}
    for f in sorted((KB / "people" / user / "progress").glob("week-*.md")):
        text = f.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(
                r"^## (\w{3}) (\w{3}) (\d{1,2})[^\n]*?— (.+?)\s*(?:\[status: ([\w-]+)\])?\s*$\n(.*?)(?=^## |\Z)",
                text, re.S | re.M):
            _dow, mon, dd, title, status, body = m.groups()
            if mon not in MONTHS:
                continue
            d = date(2026, MONTHS[mon], int(dd)).isoformat()
            subtasks = [{"text": t.strip(), "done": x.lower() == "x"}
                        for x, t in re.findall(r"^- \[( |x|X)\] (.+)$", body, re.M)]
            out[d] = {"status": (status or "not-started").strip(),
                      "title": title.strip(), "subtasks": subtasks}
    return out


def app_state_file(user):
    return DATA / f"{user}.json"


def load_app_claims(user):
    f = app_state_file(user)
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return {"claims": {}}  # date -> {"ts": iso}


def save_app_claims(user, obj):
    DATA.mkdir(exist_ok=True)
    app_state_file(user).write_text(json.dumps(obj, indent=2), encoding="utf-8")


# ------------------------------------------------------------------- cloud --
_cloud = {"rows": [], "fetched": 0.0, "ok": False, "pushed": set()}
_cloud_lock = threading.Lock()
CLOUD_TTL = 15  # seconds


def supa(method, path, body=None):
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"apikey": SUPABASE_KEY,
                 "Authorization": f"Bearer {SUPABASE_KEY}",
                 "Content-Type": "application/json",
                 "Prefer": "resolution=merge-duplicates,return=minimal"})
    with urllib.request.urlopen(req, timeout=6) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def cloud_rows(force=False):
    """All scoreboard rows, cached briefly. [] + ok=False when offline/no table."""
    if not CLOUD_ENABLED:
        return [], False
    with _cloud_lock:
        if not force and time.time() - _cloud["fetched"] < CLOUD_TTL:
            return _cloud["rows"], _cloud["ok"]
        try:
            rows = supa("GET", "claims?select=*") or []
            _cloud.update(rows=rows, ok=True, fetched=time.time())
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            _cloud.update(ok=False, fetched=time.time())
        return _cloud["rows"], _cloud["ok"]


def cloud_push(user, iso, kind, xp, source):
    """Upsert one scoreboard row; silently skipped when offline."""
    if not CLOUD_ENABLED:
        return False
    try:
        supa("POST", "claims?on_conflict=username,date",
             [{"username": user, "date": iso, "kind": kind, "xp": xp, "source": source}])
        with _cloud_lock:
            _cloud["fetched"] = 0.0     # next read sees it
        return True
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return False


def cloud_sync_own(user, schedule, done):
    """Make sure every locally-known done day for THIS user is in the cloud,
    so the rival sees KB-marked progress without waiting for a git pull."""
    rows, ok = cloud_rows()
    if not ok:
        return
    have = {r["date"] for r in rows if r["username"] == user}
    missing = [d for d in done if d not in have and d not in _cloud["pushed"]]
    for iso in missing[:40]:
        info = schedule.get(iso)
        kind = info["kind"] if info else "study"
        xp = XP_BUILD if kind == "build" else XP_WEEKDAY
        if cloud_push(user, iso, kind, xp, "kb"):
            _cloud["pushed"].add(iso)


def user_stats(user, schedule, today, extra_done=None):
    """Merge KB progress + app claims (+ cloud rows) into done set, XP, streak."""
    prog = load_progress(user)
    claims = load_app_claims(user)["claims"]
    done = {d for d, p in prog.items() if p["status"] == "done"} | set(claims)
    if extra_done:
        done |= extra_done
    xp = 0
    for d in done:
        info = schedule.get(d)
        xp += XP_BUILD if info and info["kind"] == "build" else XP_WEEKDAY
    # streak: consecutive scheduled days done, walking back from today
    # (today itself doesn't break the streak while still unclaimed)
    streak = 0
    d = today
    if d.isoformat() not in done:
        d -= timedelta(days=1)
    while d >= PLAN_START:
        iso = d.isoformat()
        if iso in schedule:            # Sundays aren't scheduled -> skipped
            if iso in done:
                streak += 1
            else:
                break
        d -= timedelta(days=1)
    rank = RANKS[0][1]
    level = 1
    for i, (th, name) in enumerate(RANKS):
        if xp >= th:
            rank, level = name, i + 1
    return {"xp": xp, "streak": streak, "level": level, "rank": rank,
            "done": sorted(done), "progress": prog}


def week_xp(user_done, schedule, week_no):
    xp = 0
    for d in user_done:
        info = schedule.get(d)
        if info and info["week"] == week_no:
            xp += XP_BUILD if info["kind"] == "build" else XP_WEEKDAY
    return xp


# ------------------------------------------------------------------- notes --
def note_path(user, iso):
    return KB / "people" / user / "notes" / f"{iso}.md"


def read_note(user, iso):
    p = note_path(user, iso)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def write_note(user, iso, content):
    p = note_path(user, iso)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def recent_notes(user, limit=6):
    d = KB / "people" / user / "notes"
    if not d.exists():
        return []
    files = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for f in files[:limit]:
        first = ""
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                first = line.strip().lstrip("# ")[:80]
                break
        out.append({"file": f.name, "path": str(f.relative_to(KB)).replace("\\", "/"),
                    "first_line": first,
                    "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="minutes")})
    return out


def kb_feed(limit=6):
    """Recently modified markdown across the KB (wiki, people, log)."""
    hits = []
    for pattern in ("wiki/**/*.md", "people/*/progress/*.md", "people/*/notes/*.md", "log.md"):
        for f in KB.glob(pattern):
            hits.append(f)
    hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for f in hits[:limit]:
        out.append({"path": str(f.relative_to(KB)).replace("\\", "/"),
                    "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="minutes")})
    return out


# -------------------------------------------------------------------- chat --
# antigravity = Google's agy CLI (successor of the retired gemini CLI)
PROVIDERS = {"claude": "Claude", "codex": "Codex", "antigravity": "Antigravity"}
PROVIDER_BIN = {"claude": "claude", "codex": "codex", "antigravity": "agy"}
AGY_BRAIN = Path.home() / ".gemini" / "antigravity-cli" / "brain"
_agy_lock = threading.Lock()


def providers_available():
    return {k: bool(shutil.which(b)) for k, b in PROVIDER_BIN.items()}


def chats_file():
    return DATA / "chats.json"


def load_chats():
    f = chats_file()
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"convos": []}


def save_chats(obj):
    DATA.mkdir(exist_ok=True)
    chats_file().write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


GROUNDING_SYS = (
    f"You are the in-app tutor of ARC/180, answering inside the {KB_NAME} repo "
    "(an LLM wiki holding a multi-month study plan; read its CLAUDE.md or "
    "README.md for conventions). This is an ongoing session that lasts the whole "
    "study day: later messages are follow-ups, keep the context. Answer concisely "
    "and practically, grounded in this repo's wiki/, schedule/ and people/ files "
    "where relevant. End each answer with a 'Sources:' line listing the repo file "
    "paths you used (or 'none').")

GROUNDING = GROUNDING_SYS + " Question: "


def _exec(argv):
    try:
        # stdin MUST be closed: codex (and possibly others) block reading
        # "additional input from stdin" when they inherit an open pipe.
        r = subprocess.run(argv, cwd=str(KB), capture_output=True, text=True,
                           stdin=subprocess.DEVNULL,
                           timeout=300, encoding="utf-8", errors="replace")
        out = (r.stdout or "").strip()
        if r.returncode != 0 and not out:
            return False, (r.stderr or "").strip()[:2000] or f"exit code {r.returncode}"
        return True, out
    except subprocess.TimeoutExpired:
        return False, "The CLI took longer than 5 minutes and was stopped."


def _agy_convos():
    if not AGY_BRAIN.exists():
        return set()
    return {p.name for p in AGY_BRAIN.iterdir() if p.is_dir()}


def run_chat(provider, message, session):
    """One app conversation == one persistent CLI session.

    Returns (ok, reply, session_id). session is None on the first message;
    the returned id is stored on the convo and reused for every follow-up.
    """
    exe = shutil.which(PROVIDER_BIN.get(provider, ""))
    if not exe:
        return False, f"The {PROVIDERS[provider]} CLI is not on PATH on this PC.", session
    first = session is None
    prompt = (GROUNDING + message) if first else message

    if provider == "claude":
        sid = session or str(uuid.uuid4())
        argv = [exe, "-p", message, "--allowedTools", "Read,Glob,Grep"]
        if first:
            argv += ["--session-id", sid, "--append-system-prompt", GROUNDING_SYS]
        else:
            argv += ["--resume", sid]
        ok, out = _exec(argv)
        return ok, out, (sid if ok else session)

    if provider == "codex":
        # this codex install is missing its Windows sandbox helper
        # (codex-windows-sandbox-setup.exe), so any sandboxed tool call fails;
        # danger-full-access skips the sandbox entirely. Local, personal use.
        sandbox = ["--sandbox", "danger-full-access"]
        if first:
            ok, out = _exec([exe, "exec", "--json", *sandbox, prompt])
            if not ok:
                return False, out, None
            sid, text = None, ""
            for line in out.splitlines():
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "thread.started":
                    sid = ev.get("thread_id") or ev.get("session_id")
                item = ev.get("item") or {}
                if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
                    text = item.get("text", "")
            return True, (text or out), sid
        # `exec resume` rejects --sandbox; the config override works instead
        ok, out = _exec([exe, "exec", "resume", session,
                         "-c", 'sandbox_mode="danger-full-access"', prompt])
        return ok, out, session

    if provider == "antigravity":
        # agy -p never prints its conversation id (antigravity-cli issue #7),
        # so on the first message we diff ~/.gemini/antigravity-cli/brain/
        # before/after the run to learn which conversation was created.
        flags = ["--sandbox", "--dangerously-skip-permissions"]
        if first:
            with _agy_lock:
                before = _agy_convos()
                ok, out = _exec([exe, "-p", prompt, *flags])
                new = _agy_convos() - before
            sid = next(iter(new)) if len(new) == 1 else None
            return ok, out, (sid if ok else None)
        ok, out = _exec([exe, "--conversation", session, "-p", prompt, *flags])
        return ok, out, session

    return False, "unknown provider", session


# --------------------------------------------------------------- state api --
def build_state(user):
    today = date.today()
    schedule, weeks = load_schedule()
    rows, cloud_ok = cloud_rows()
    from_cloud = {u: {r["date"] for r in rows if r["username"] == u} for u in USERS}
    stats = {u: user_stats(u, schedule, today, extra_done=from_cloud.get(u))
             for u in USERS}
    cloud_sync_own(user, schedule, stats[user]["done"])

    iso = today.isoformat()
    info = schedule.get(iso)
    day_n = (today - PLAN_START).days + 1
    prog_today = stats[user]["progress"].get(iso)
    today_block = {
        "date": iso,
        "pretty": today.strftime("%A, %B %d").replace(" 0", " "),
        "day_n": day_n, "plan_days": PLAN_DAYS,
        "scheduled": bool(info),
        "title": (info or {}).get("title", "Rest / review day"),
        "task": (info or {}).get("task", "No scheduled task today. Review the week, promote notes into the wiki, or take the day."),
        "week": (info or {}).get("week"),
        "week_title": (info or {}).get("week_title", ""),
        "month_title": (info or {}).get("month_title", ""),
        "kind": (info or {}).get("kind", "rest"),
        "xp": XP_BUILD if info and info["kind"] == "build" else XP_WEEKDAY,
        "done": iso in stats[user]["done"],
        "subtasks": (prog_today or {}).get("subtasks", []),
    }

    # calendar for current month
    first = today.replace(day=1)
    cal_days = {}
    d = first
    while d.month == first.month:
        di = d.isoformat()
        entry = {"topic": "", "kind": "rest"}
        if di in schedule:
            entry = {"topic": schedule[di]["title"], "kind": schedule[di]["kind"]}
        elif d.weekday() == 6 and PLAN_START <= d:
            entry = {"topic": "Review", "kind": "review"}
        entry["status"] = {u: (di in stats[u]["done"]) for u in USERS}
        cal_days[di] = entry
        d += timedelta(days=1)

    # weekly duels
    duels = []
    cur_week = info["week"] if info else None
    seen = set()
    for w in weeks:
        if w["week"] in seen or w["week"] == 0:
            continue
        seen.add(w["week"])
        m_xp = week_xp(stats[USERS[0]]["done"], schedule, w["week"])
        a_xp = week_xp(stats[USERS[1]]["done"], schedule, w["week"])
        started = any(s["week"] == w["week"] and s_date <= iso
                      for s_date, s in schedule.items())
        if not started:
            continue
        duels.append({"week": w["week"], "title": w["title"], "m": m_xp, "a": a_xp,
                      "live": cur_week == w["week"]})

    chats = load_chats()
    convo_meta = [{"id": c["id"], "title": c["title"], "provider": c["provider"],
                   "when": c["updated"], "count": len(c["messages"])}
                  for c in sorted(chats["convos"], key=lambda c: c["updated"], reverse=True)]

    return {
        "user": user,
        "rival": next(u for u in USERS if u != user),
        "today": today_block,
        "users": {u: {k: v for k, v in stats[u].items() if k != "progress"} for u in USERS},
        "calendar": {"year": today.year, "month": today.month,
                     "month_name": today.strftime("%B %Y"), "days": cal_days},
        "weeks": [w for w in weeks if w["week"] and w["week"] >= (cur_week or 1)][:3],
        "note": read_note(user, iso),
        "recent_notes": recent_notes(user),
        "kb_feed": kb_feed(),
        "duels": duels,
        "convos": convo_meta,
        "providers": providers_available(),
        "cloud": {"connected": cloud_ok, "url": SUPABASE_URL, "anon_key": SUPABASE_KEY},
        "kb_name": KB_NAME,
        "users_order": USERS,
        "plan": {"start": PLAN_START.isoformat(), "days": PLAN_DAYS,
                 "progress_pct": {u: round(100 * len(stats[u]["done"]) / PLAN_DAYS, 1)
                                  for u in USERS}},
    }


# ------------------------------------------------------------------ server --
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path):
        f = (WEB / path.lstrip("/")).resolve()
        if not str(f).startswith(str(WEB.resolve())) or not f.is_file():
            self.send_error(404)
            return
        ctypes = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
                  ".css": "text/css", ".json": "application/json",
                  ".webmanifest": "application/manifest+json",
                  ".png": "image/png", ".woff2": "font/woff2", ".svg": "image/svg+xml"}
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctypes.get(f.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/api/state":
            with _lock:
                return self._json(build_state(q.get("user") or whoami()))
        if u.path == "/api/note":
            return self._json({"content": read_note(q.get("user") or whoami(),
                                                    q.get("date", date.today().isoformat()))})
        if u.path == "/api/convo":
            chats = load_chats()
            for c in chats["convos"]:
                if c["id"] == q.get("id"):
                    return self._json(c)
            return self._json({"error": "not found"}, 404)
        if u.path == "/":
            return self._static("index.html")
        return self._static(u.path)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)
        user = body.get("user") or whoami()

        if self.path == "/api/claim":
            iso = body.get("date", date.today().isoformat())
            with _lock:
                obj = load_app_claims(user)
                obj["claims"][iso] = {"ts": datetime.now().isoformat(timespec="seconds")}
                save_app_claims(user, obj)
                schedule, _w = load_schedule()
                info = schedule.get(iso)
                kind = info["kind"] if info else "study"
                cloud_push(user, iso, kind,
                           XP_BUILD if kind == "build" else XP_WEEKDAY, "app")
                return self._json(build_state(user))

        if self.path == "/api/note":
            iso = body.get("date", date.today().isoformat())
            write_note(user, iso, body.get("content", ""))
            return self._json({"ok": True, "saved": datetime.now().isoformat(timespec="seconds")})

        if self.path == "/api/chat":
            provider = body.get("provider", "claude")
            message = (body.get("message") or "").strip()
            if provider not in PROVIDERS:
                return self._json({"error": "unknown provider"}, 400)
            if not message:
                return self._json({"error": "empty message"}, 400)
            # continuing a convo keeps its provider + CLI session
            session = None
            with _lock:
                chats = load_chats()
                convo = None
                if body.get("convo_id"):
                    convo = next((c for c in chats["convos"] if c["id"] == body["convo_id"]), None)
                if convo:
                    provider = convo["provider"]
                    session = convo.get("session")
            ok, reply, session = run_chat(provider, message, session)
            with _lock:
                chats = load_chats()
                convo = None
                if body.get("convo_id"):
                    convo = next((c for c in chats["convos"] if c["id"] == body["convo_id"]), None)
                if convo is None:
                    convo = {"id": uuid.uuid4().hex[:10],
                             "title": message[:60], "provider": provider,
                             "messages": []}
                    chats["convos"].append(convo)
                if ok and session:
                    convo["session"] = session
                convo["messages"].append({"role": "user", "text": message})
                convo["messages"].append({"role": "assistant", "text": reply,
                                          "ok": ok, "provider": provider})
                convo["updated"] = datetime.now().isoformat(timespec="seconds")
                save_chats(chats)
            return self._json({"ok": ok, "reply": reply, "convo_id": convo["id"]})

        return self._json({"error": "not found"}, 404)


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def keep_alive():
    """Free Supabase projects pause after ~7 idle days (it happened on 2026-07-18
    → 07-26 and took the scoreboard offline). A cheap ping every 6h prevents it."""
    while CLOUD_ENABLED:
        cloud_rows(force=True)
        time.sleep(6 * 3600)


if __name__ == "__main__":
    DATA.mkdir(exist_ok=True)
    threading.Thread(target=keep_alive, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    if sys.stdout:  # absent under pythonw.exe (the hidden autostart)
        print("ARC/180 running:")
        print(f"  PC     -> http://localhost:{PORT}")
        print(f"  Phone  -> http://{lan_ip()}:{PORT}  (same Wi-Fi)")
    srv.serve_forever()
