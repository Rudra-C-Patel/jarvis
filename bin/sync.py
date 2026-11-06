#!/usr/bin/env python3
"""Jarvis vault sync: read ~/PersonalBrain (READ-ONLY) and push it into the local Open WebUI.

What it does
  1. Builds the Jarvis system prompt = system_prompt.md + priority notes
     (Home.md, Areas/Personal/*, the most recent Daily notes) inlined verbatim.
  2. Incrementally mirrors every other vault note into an Open WebUI knowledge
     collection ("PersonalBrain") for retrieval. Only changed files are re-uploaded.
  3. Creates/updates the "Jarvis" model in Open WebUI with that prompt + knowledge.

Safety
  - The vault is only ever opened with mode "rb". Nothing writes under VAULT; a guard
    below refuses to run if any state path resolves inside the vault.
  - Only talks to 127.0.0.1 (Open WebUI). stdlib only, no third-party packages.
"""
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import date
import urllib.request
import urllib.error
from pathlib import Path

HOME = Path.home()
JARVIS = Path(__file__).resolve().parent.parent
# jarvis.env is the single source of truth, even when this script is run directly
# (real environment variables still win, e.g. for one-off overrides).
_ENV = JARVIS / "jarvis.env"
for _line in (_ENV.read_text().splitlines() if _ENV.exists() else []):
    if "=" in _line and not _line.lstrip().startswith("#"):
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.split(" #")[0].strip().strip('"'))
VAULT = Path(os.environ.get("JARVIS_VAULT", HOME / "PersonalBrain")).resolve()
STATE = Path(os.environ.get("JARVIS_SYNC_STATE", JARVIS / "data" / "sync-state.json"))
PK = Path(os.environ.get("JARVIS_PK_DIR", JARVIS / "private-knowledge")).resolve()  # ClaudeHistory (scrubbed)
KB_SUFFIX = os.environ.get("JARVIS_KB_SUFFIX", "")          # test mode: e.g. "-TEST"
SKIP_VAULT_KB = os.environ.get("JARVIS_SKIP_VAULT_KB") == "1"  # test mode: don't touch PersonalBrain KB
ALWAYS_ON_BUDGET = int(os.environ.get("JARVIS_ALWAYS_ON_TOKENS", "10000"))
PROMPT_FILE = JARVIS / "system_prompt.md"
WEBUI = os.environ.get("JARVIS_WEBUI_URL", f"http://127.0.0.1:{os.environ.get('JARVIS_PORT', '8484')}")
BASE_MODEL = os.environ.get("JARVIS_BASE_MODEL", "")
USER = os.environ.get("JARVIS_USER_NAME", "the user")
NUM_CTX = int(os.environ.get("JARVIS_NUM_CTX", "32768"))
RECENT_DAILY = int(os.environ.get("JARVIS_RECENT_DAILY", "5"))
MODEL_ID = os.environ.get("JARVIS_MODEL_ID", "jarvis")

EXCLUDE_DIRS = {".obsidian", ".git", ".gstack", ".agents", ".trash", "Templates", "node_modules"}
EXCLUDE_FILES = {"skills-lock.json"}

# ---- guard: never write inside the vault -------------------------------------------
FOLLOW_LINKS = {"Brain"}  # top-level vault symlinks to index (read-only)
LINKED_DIRS = {n: (VAULT / n).resolve() for n in sorted(FOLLOW_LINKS)
               if (VAULT / n).is_symlink() and (VAULT / n).resolve().is_dir()}

for p in (STATE, PROMPT_FILE, JARVIS, PK):
    for protected in (VAULT, *LINKED_DIRS.values()):
        if str(p.resolve()).startswith(str(protected) + os.sep) or p.resolve() == protected:
            sys.exit(f"refusing to run: {p} is inside the vault ({protected})")
if not WEBUI.startswith(("http://127.0.0.1", "http://localhost")):
    sys.exit("refusing to run: Open WebUI URL must be local")


def read_vault(rel):
    with open(VAULT / rel, "rb") as f:  # read-only
        return f.read()


def _walk(base, prefix):
    out = []
    for root, dirs, files in os.walk(base):  # followlinks=False: no symlink loops
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith("."))
        for name in sorted(files):
            if name.endswith(".md") and name not in EXCLUDE_FILES:
                out.append(str(Path(prefix) / (Path(root) / name).relative_to(base)))
    return out


def vault_notes():
    out = _walk(VAULT, "")
    # Explicitly follow whitelisted top-level symlinks (e.g. Brain/ -> ~/fable/brain), read-only.
    for name, target in LINKED_DIRS.items():
        out += _walk(target, name)
    return sorted(out)


# ---- priority context (inlined into the system prompt) ----------------------------
QUERY_BLOCK = re.compile(r"```(?:dataview|dataviewjs|tasks|base)\n.*?```\n?", re.S)


def clean(text):
    """Drop Obsidian query blocks (they're code, not facts) and collapse blank runs."""
    text = QUERY_BLOCK.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def priority_files(notes):
    home = [n for n in notes if n == "Home.md"]
    personal = [n for n in notes if n.startswith("Areas/Personal/")]
    daily = sorted((n for n in notes if n.startswith("Daily/")), reverse=True)[:RECENT_DAILY]
    return home + personal + daily


def tokens(s):
    return len(s) // 4  # rough estimate for English markdown


def current_focus(projects_md):
    m = re.search(r"^## Current focus\s*\n(.*?)(?=^## |\Z)", projects_md, flags=re.S | re.M)
    return m.group(1).strip() if m else ""


def profile_blocks():
    """Always-on context from Claude history: me.md, life-context.md, current focus (if generated)."""
    prof = PK / "profile"
    blocks = []
    for fname, title in (("me.md", f"HOW {USER.upper()} WANTS TO BE HELPED (me.md: follow this; it overrides default style)"),
                         ("life-context.md", "LIFE CONTEXT (from their own Claude history: life-context.md)")):
        f = prof / fname
        if f.exists():
            blocks.append((title, f.read_text(encoding="utf-8").strip()))
    pj = prof / "projects.md"
    if pj.exists() and current_focus(pj.read_text(encoding="utf-8")):
        blocks.append(("CURRENT FOCUS (from projects.md; full project detail arrives via ClaudeHistory excerpts)",
                       current_focus(pj.read_text(encoding="utf-8"))))
    return blocks


# ---- Recency: newest dated entries + Daily staleness (always-on) -------------------
DATE_RX = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")
TODAY = date.fromisoformat(os.environ["JARVIS_TODAY"]) if os.environ.get("JARVIS_TODAY") else date.today()  # test hook
DATED_MAX = int(os.environ.get("JARVIS_DATED_ENTRIES", "16"))
STALE_DAYS = 7
PER_FILE_MAX = 4


def dated_entries(notes):
    """Bullets/headings from non-Daily notes that carry an entry date (Meetings logs, Status notes, dated
    lines). A bullet under a dated heading inherits its date. Due dates (📅) and struck-out lines don't count."""
    out = []
    for rel in notes:
        if rel.startswith(("Daily/", "Archives/", "Templates/")) or re.search(r"(?i)(^|/|-)index\.md$", rel):
            continue  # dailies are inlined already; index notes are link lists, not facts
        head_date = None
        for i, line in enumerate(clean(read_vault(rel).decode("utf-8", "replace")).splitlines()):
            st = line.strip()
            if st.startswith("#"):
                m = DATE_RX.search(st)
                head_date = m.group(1) if m else (None if st.startswith("## ") else head_date)
                continue
            if not st.startswith(("- ", "* ")) or "~~" in st or st.startswith(("- [ ]", "* [ ]")):
                continue
            m = DATE_RX.search(st.split("📅")[0][:60])  # the entry's own date sits near the start
            d = m.group(1) if m else head_date
            if d and d <= TODAY.isoformat():
                out.append((d, i, rel, st[2:].strip()))
    out.sort(key=lambda e: (e[0], e[1]), reverse=True)  # same date: later lines in a log are newer
    seen, per_file, top = set(), {}, []
    for d, _, rel, text in out:
        if text not in seen and per_file.get(rel, 0) < PER_FILE_MAX:  # spread across notes
            seen.add(text)
            per_file[rel] = per_file.get(rel, 0) + 1
            top.append((d, rel, text if len(text) <= 220 else text[:217] + "..."))
        if len(top) >= DATED_MAX:
            break
    return top


def recency_block(notes):
    dailies = sorted(n for n in notes if re.fullmatch(r"Daily/\d{4}-\d\d-\d\d\.md", n))
    lines = []
    if dailies:
        nd = dailies[-1][6:16]
        age = (TODAY - date.fromisoformat(nd)).days
        lines.append(f"- Newest Daily note: {nd} ({age} days old when synced on {TODAY.isoformat()}).")
        if age > STALE_DAYS:
            lines.append(f"- STALE: the newest Daily note is more than {STALE_DAYS} days old. Start any answer about "
                         f"today, status or what's open with: \"Heads up: your newest Daily note is {age} days old ({nd}).\"")
    else:
        lines.append("- No Daily notes found.")
    lines.append("- Newest dated entries across the vault (newest first; these beat older undated or earlier-dated "
                 "lines on the same topic):")
    lines += [f"  - {d} [{rel}] {t}" for d, rel, t in dated_entries(notes)]
    return "\n".join(lines)


LAST_PROMPT_INFO = {}


def build_system_prompt(notes):
    head = [PROMPT_FILE.read_text(encoding="utf-8").strip()]
    for title, body in profile_blocks():
        head += ["", "---", f"# {title}", "", body]
    head += ["", "---", "# RECENCY (computed at sync; use it to resolve conflicts)", "", recency_block(notes)]
    pri = priority_files(notes)
    vault = {rel: clean(read_vault(rel).decode("utf-8", "replace")) for rel in pri}
    # Budget: drop the oldest inlined Daily notes first (they stay searchable), keep at least one.
    dailies = [r for r in pri if r.startswith("Daily/")]
    while True:
        parts = head + ["", "---", "# VAULT CONTEXT (priority notes, verbatim)"]
        for rel in pri:
            parts += [f"\n## SOURCE: {rel}\n", vault[rel]]
        parts.append("\n---\nOther vault notes and past Claude conversations arrive as retrieved excerpts "
                     "(SOURCE: PersonalBrain/... or ClaudeHistory/...). If something isn't in any of these, say so.")
        prompt = "\n".join(parts)
        if tokens(prompt) <= ALWAYS_ON_BUDGET or len(dailies) <= 1:
            break
        pri.remove(dailies.pop())  # dailies sorted newest-first -> pop oldest
    LAST_PROMPT_INFO.update(tokens=tokens(prompt), inlined=len(pri), profiles=len(profile_blocks()),
                            over_budget=tokens(prompt) > ALWAYS_ON_BUDGET)
    return prompt


# ---- Open WebUI API ---------------------------------------------------------------
TOKEN = None


def api(method, path, body=None, files=None, query=""):
    url = f"{WEBUI}/api/v1{path}{query}"
    headers = {"Accept": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    data = None
    if files:
        boundary = uuid.uuid4().hex
        chunks = []
        for field, (fname, content, ctype) in files.items():
            chunks += [f"--{boundary}\r\n".encode(),
                       f'Content-Disposition: form-data; name="{field}"; filename="{fname}"\r\n'.encode(),
                       f"Content-Type: {ctype}\r\n\r\n".encode(), content, b"\r\n"]
        for field, value in (body or {}).items():
            chunks += [f"--{boundary}\r\n".encode(),
                       f'Content-Disposition: form-data; name="{field}"\r\n\r\n'.encode(),
                       value.encode(), b"\r\n"]
        chunks.append(f"--{boundary}--\r\n".encode())
        data = b"".join(chunks)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=3600 if files else 300) as r:  # big files embed synchronously
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode()[:300]}") from None


def login():
    global TOKEN
    # Single-user local mode (WEBUI_AUTH=False): Open WebUI's built-in local admin.
    TOKEN = api("POST", "/auths/signin", {"email": "admin@localhost", "password": "admin"})["token"]


def ensure_kb(kstate, name, description):
    kb_id = kstate.get("kb_id")
    if kb_id:
        try:
            api("GET", f"/knowledge/{kb_id}")
            return kb_id
        except RuntimeError:
            kstate["files"] = {}
    kb = api("POST", "/knowledge/create", {"name": name, "description": description})
    kstate["kb_id"], kstate["files"] = kb["id"], {}
    return kb["id"]


def upload_note(kb_id, rel, content, source="PersonalBrain"):
    # Header lets the model cite the source path; the source file itself is untouched.
    body = f"SOURCE: {source}/{rel}\n\n".encode() + content
    fname = f"{source} - " + rel.replace("/", " - ") if source != "PersonalBrain" else rel.replace("/", " - ")
    f = api("POST", "/files/", files={"file": (fname, body, "text/markdown")},
            query="?process=true&process_in_background=false")
    api("POST", f"/knowledge/{kb_id}/file/add", {"file_id": f["id"]})
    return f["id"]


def remove_note(kb_id, file_id):
    try:
        api("POST", f"/knowledge/{kb_id}/file/remove", {"file_id": file_id}, query="?delete_file=true")
    except RuntimeError as e:
        print(f"  (remove skipped: {e})")


def upsert_model(prompt, kb_ids):
    kbs = [api("GET", f"/knowledge/{i}") for i in kb_ids]
    form = {
        "id": MODEL_ID,
        "base_model_id": BASE_MODEL,
        "name": "Jarvis" + (f" ({MODEL_ID})" if MODEL_ID != "jarvis" else ""),
        "meta": {
            "description": "Private personal-life assistant. 100% local. Reads your PersonalBrain vault and Claude history.",
            "knowledge": [{**{k: kb[k] for k in ("id", "name", "description") if k in kb}, "type": "collection"} for kb in kbs],
            "capabilities": {"citations": True, "file_upload": True, "vision": False,
                             "web_search": False, "image_generation": False, "code_interpreter": False},
        },
        "params": {"system": prompt, "num_ctx": NUM_CTX, "function_calling": "legacy", "temperature": 0.6, "top_p": 0.95, "top_k": 20},
        "access_grants": [],
        "is_active": True,
    }
    try:
        api("GET", f"/models/model?id={MODEL_ID}")
        api("POST", f"/models/model/update?id={MODEL_ID}", form)
        return "updated"
    except RuntimeError:
        api("POST", "/models/create", form)
        return "created"


PROMPTS = {  # command -> (title, file)
    "checkin": ("Daily check-in", JARVIS / "prompts" / "daily-checkin.md"),
}


def upsert_prompts():
    existing = {p["command"].lstrip("/"): p for p in (api("GET", "/prompts/") or [])}
    for cmd, (title, path) in PROMPTS.items():
        form = {"command": cmd, "name": title, "content": path.read_text(encoding="utf-8"),
                "tags": ["jarvis"], "access_grants": []}
        if cmd in existing:
            if existing[cmd].get("content") != form["content"] or existing[cmd].get("name") != title:
                api("POST", f"/prompts/id/{existing[cmd]['id']}/update", form)
        else:
            api("POST", "/prompts/create", form)


def enforce_local_voice():
    """Pin per-user audio engines to the server default (local Whisper STT; on-device TTS voices),
    so a stray 'Web API' choice in Settings > Audio can't route speech through the browser/Google."""
    s = api("GET", "/users/user/settings", query="?raw=true") or {}
    ui = s.get("ui") or {}
    audio = ui.get("audio") or {}
    want = {"stt": {**(audio.get("stt") or {}), "engine": ""},
            "tts": {**(audio.get("tts") or {}), "engine": ""}}
    if audio.get("stt") != want["stt"] or audio.get("tts") != want["tts"]:
        ui["audio"] = {**audio, **want}
        api("POST", "/users/user/settings/update", {**s, "ui": ui})


def sync_collection(state, name, description, sources, source_label):
    """sources: {rel_path: absolute Path}. Incremental by sha256; mirrors deletions."""
    kstate = state.setdefault("kbs", {}).setdefault(name, {})
    kb_id = ensure_kb(kstate, name, description)
    known = kstate.setdefault("files", {})
    added = changed = removed = 0
    for rel, path in sources.items():
        if isinstance(path, bytes):  # pre-split part of a large file
            content = path
        else:
            with open(path, "rb") as f:  # read-only
                content = f.read()
        digest = hashlib.sha256(content).hexdigest()
        prev = known.get(rel)
        if prev and prev["sha"] == digest:
            continue
        if prev:
            remove_note(kb_id, prev["file_id"])
            changed += 1
        else:
            added += 1
        if not content.strip():
            known.pop(rel, None)
            continue
        known[rel] = {"sha": digest, "file_id": upload_note(kb_id, rel, content, source_label)}
        save_state(state)  # checkpoint (in ~/jarvis, never the vault)
    for rel in [r for r in known if r not in sources]:
        remove_note(kb_id, known.pop(rel)["file_id"])
        removed += 1
    return kb_id, f"{name}: {len(sources)} docs (+{added} ~{changed} -{removed})"


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1))


PART_BYTES = 400_000  # split huge chats at message boundaries: faster, resumable uploads + precise citations


def split_parts(path):
    text = path.read_text(encoding="utf-8")
    head = text.split("\n## ", 1)[0]  # title/date block, repeated on every part
    msgs = ["## " + m for m in text.split("\n## ")[1:]]
    parts, cur = [], ""
    for m in msgs:
        if cur and len(cur.encode()) + len(m.encode()) > PART_BYTES:
            parts.append(cur)
            cur = ""
        cur += m + "\n"
    parts.append(cur)
    n = len(parts)
    return {f"conversations/{path.stem}__part{i + 1:02d}of{n:02d}.md":
            f"{head}\nPart {i + 1} of {n} (split for indexing)\n\n{p}".encode() for i, p in enumerate(parts)}


def history_sources():
    conv, prof = PK / "conversations", PK / "profile"
    src = {}
    for p in sorted(conv.glob("*.md")) if conv.is_dir() else []:
        if p.stat().st_size > PART_BYTES:
            src.update(split_parts(p))
        else:
            src[f"conversations/{p.name}"] = p
    if (prof / "projects.md").exists():
        src["projects.md"] = prof / "projects.md"
    return src


def main():
    if not BASE_MODEL:
        sys.exit("JARVIS_BASE_MODEL is not set (copy jarvis.env.example to jarvis.env)")
    if not VAULT.is_dir():
        sys.exit(f"vault not found: {VAULT}")
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if "kb_id" in state:  # migrate v1.x single-collection state
        state.setdefault("kbs", {})["PersonalBrain"] = {"kb_id": state.pop("kb_id"), "files": state.pop("files", {})}
    notes = vault_notes()
    login()

    kb_ids, report = [], []
    if not SKIP_VAULT_KB:
        kid, r = sync_collection(state, "PersonalBrain" + KB_SUFFIX,
                                 "Read-only mirror of ~/PersonalBrain (synced by ~/jarvis/bin/sync.py)",
                                 {rel: VAULT / rel for rel in notes}, "PersonalBrain")
        kb_ids.append(kid)
        report.append(r)
    hist = history_sources()
    if hist:
        kid, r = sync_collection(state, "ClaudeHistory" + KB_SUFFIX,
                                 "Scrubbed Claude conversation history + projects.md (~/jarvis/private-knowledge)",
                                 hist, "ClaudeHistory")
        kb_ids.append(kid)
        report.append(r)

    prompt = build_system_prompt(notes)
    action = upsert_model(prompt, kb_ids)
    upsert_prompts()
    enforce_local_voice()
    save_state(state)
    i = LAST_PROMPT_INFO
    print(f"{' | '.join(report)} | always-on: {i['profiles']} profile blocks + {i['inlined']} vault notes "
          f"(~{i['tokens']} tokens{', OVER BUDGET' if i['over_budget'] else ''}) | model {MODEL_ID} {action} on {BASE_MODEL}")


if __name__ == "__main__":
    main()
