#!/usr/bin/env python3
"""Jarvis ClaudeHistory pipeline: Claude data export -> scrubbed markdown -> summaries -> profiles.

usage: history.py [ingest|summarize|profiles|all|status] [--zip PATH] [--force]

  ingest     newest Claude export zip in ~/Downloads -> private-knowledge/raw/ (700, temporary) and
             private-knowledge/conversations/*.md (scrubbed). Incremental per conversation.
  summarize  3-6 line summary + project tags per conversation (local Ollama), incremental.
  profiles   profile/projects.md, profile/life-context.md, profile/me.md (local Ollama).
             Regenerated only when their inputs changed.
  all        ingest + summarize + profiles

Privacy: reads only the export zip; writes only under JARVIS_PK_DIR (default
~/jarvis/private-knowledge, chmod 700, files 600). Refuses to run if that dir is inside
~/PersonalBrain or ~/fable. All LLM calls go to Ollama on 127.0.0.1. stdlib only.
Secrets are scrubbed (scrub.py) before anything except raw/ is written; counts only are reported.
"""
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.request
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrub import scrub  # noqa: E402

JARVIS = Path(__file__).resolve().parent.parent
_ENV = JARVIS / "jarvis.env"
for _line in (_ENV.read_text().splitlines() if _ENV.exists() else []):  # same config as sync.py
    if "=" in _line and not _line.lstrip().startswith("#"):
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.split(" #")[0].strip().strip('"'))

HOME = Path.home()
PK = Path(os.environ.get("JARVIS_PK_DIR", JARVIS / "private-knowledge")).resolve()
DOWNLOADS = Path(os.environ.get("JARVIS_DOWNLOADS", HOME / "Downloads")).resolve()
RAW, CONV, SUMM, PROF = PK / "raw", PK / "conversations", PK / "summaries", PK / "profile"
STATE_FILE = PK / "history-state.json"
OLLAMA = "http://127.0.0.1:11434"
MODEL = os.environ.get("JARVIS_SUMMARY_MODEL") or os.environ.get("JARVIS_BASE_MODEL", "")
USER = os.environ.get("JARVIS_USER_NAME") or "the user"
# Optional: one personal chat that life-context.md is built from (unset = skip life-context.md).
CORE_TITLE = os.environ.get("JARVIS_CORE_TITLE", "")
CORE_DATE = os.environ.get("JARVIS_CORE_DATE", "")
PROJECTS = [p.strip() for p in os.environ.get("JARVIS_PROJECTS", "Work,School,Side projects").split(",") if p.strip()]
GPA_MIN = float(os.environ["JARVIS_GPA_MIN"]) if os.environ.get("JARVIS_GPA_MIN") else None
TAGS = PROJECTS + ["Personal", "Other"]
CHUNK = 48_000  # chars per LLM pass (~12k tokens), leaves room in a 32k context

# ---- safety ------------------------------------------------------------------------
for protected in (HOME / "PersonalBrain", HOME / "fable"):
    pr = protected.resolve() if protected.exists() else protected
    if PK == pr or str(PK).startswith(str(pr) + os.sep):
        sys.exit(f"refusing to run: {PK} is inside {protected} (that syncs to GitHub)")
os.umask(0o077)


def secure_dirs():
    for d in (PK, RAW, CONV, SUMM, PROF):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    gi = JARVIS / ".gitignore"
    lines = gi.read_text().splitlines() if gi.exists() else []
    for entry in ("private-knowledge/", "data/", "logs/"):
        if entry not in lines:
            lines.append(entry)
    gi.write_text("\n".join(lines) + "\n")


def write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)


def load_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(st):
    write_private(STATE_FILE, json.dumps(st, indent=1))


def sha(s):
    return hashlib.sha256(s.encode() if isinstance(s, str) else s).hexdigest()


# ---- 1. ingest ---------------------------------------------------------------------
PART_CATEGORIES = ("conversations", "projects", "memories", "light_metadata")


def find_export(explicit=None):
    """Return the newest export as a list of zips: a single classic export zip, or the parts of a
    multi-part export (conversations-000.zip + projects/memories/light_metadata parts, per manifest)."""
    cands = [Path(explicit)] if explicit else sorted(DOWNLOADS.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for z in cands:
        try:
            with zipfile.ZipFile(z) as zf:
                if not any(n.replace("\\", "/").split("/")[-1] == "conversations.json" for n in zf.namelist()):
                    continue
        except zipfile.BadZipFile:
            continue
        parts = [z]
        if re.match(r"conversations-\d+\.zip$", z.name):  # multi-part export
            manifests = sorted(DOWNLOADS.glob("manifest-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            names = []
            if manifests:
                try:
                    names = [f["filename"] for f in json.loads(manifests[0].read_text())["data_files"]
                             if f.get("category") in PART_CATEGORIES]
                except (KeyError, ValueError):
                    names = []
            if not names:  # no manifest: siblings downloaded within a day of the conversations part
                names = [p.name for c in PART_CATEGORIES for p in DOWNLOADS.glob(f"{c}-*.zip")
                         if abs(p.stat().st_mtime - z.stat().st_mtime) < 86400]
            parts = sorted({DOWNLOADS / n for n in names if (DOWNLOADS / n).exists()} | {z})
        return parts
    return None


def export_name(parts):
    return parts[0].name if len(parts) == 1 else "multipart:" + "+".join(p.name for p in parts)


def extract(parts):
    dest = RAW / (parts[0].stem if len(parts) == 1 else f"export-{datetime.fromtimestamp(parts[0].stat().st_mtime):%Y-%m-%d}")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    os.chmod(dest, 0o700)
    for z in parts:
        _extract_one(z, dest / z.stem)
    return dest


def _extract_one(z, dest):
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    with zipfile.ZipFile(z) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            parts = [p for p in name.split("/") if p not in ("", ".", "..")]
            if info.is_dir() or not parts:
                continue
            out = dest.joinpath(*parts)  # no path traversal: '..' parts dropped
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(out, "wb") as f:
                shutil.copyfileobj(src, f)
            os.chmod(out, 0o600)
    return dest


def find_json(root, name):
    hits = sorted(root.rglob(name))
    return json.loads(hits[0].read_text(encoding="utf-8")) if hits else None


def load_many(root, list_name, dir_name):
    """Classic exports have one <x>.json list; multi-part ones have <x>/<uuid>.json objects."""
    out = []
    for f in sorted(root.rglob(list_name)):
        v = json.loads(f.read_text(encoding="utf-8"))
        out += v if isinstance(v, list) else [v]
    for f in sorted(root.rglob(f"{dir_name}/*.json")):
        v = json.loads(f.read_text(encoding="utf-8"))
        out += v if isinstance(v, list) else [v]
    return out


def slug(s, n=60):
    s = re.sub(r"[^A-Za-z0-9]+", "-", s or "untitled").strip("-").lower()
    return (s or "untitled")[:n]


def block_text(b):
    t = b.get("type")
    if t == "text":
        return b.get("text", "")
    if t == "tool_use":
        inp = json.dumps(b.get("input", {}), ensure_ascii=False)
        return f"[tool call: {b.get('name', '?')}] {inp[:600]}"
    if t == "tool_result":
        c = b.get("content")
        if isinstance(c, list):
            c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
        return f"[tool result] {str(c or '')[:2000]}"
    return ""  # thinking, images, etc. are skipped


def conv_to_md(c, me):
    title = (c.get("name") or "").strip() or "(untitled)"
    created, updated = (c.get("created_at") or "")[:10], (c.get("updated_at") or "")[:10]
    msgs = c.get("chat_messages") or []
    out = [f"# {title}", "", f"Date: {created}" + (f" (last message {updated})" if updated and updated != created else ""),
           f"Conversation ID: {c.get('uuid', '')}", f"Messages: {len(msgs)}", ""]
    if c.get("summary"):
        out += [f"Claude's summary: {c['summary']}", ""]
    for m in msgs:
        who = me if m.get("sender") == "human" else "Claude"
        ts = (m.get("created_at") or "")[:16].replace("T", " ")
        blocks = m.get("content") or []
        text = "\n\n".join(t for t in (block_text(b) for b in blocks if isinstance(b, dict)) if t.strip()) or m.get("text", "")
        parts = [f"## {who} · {ts}", "", text.strip()]
        for a in m.get("attachments") or []:
            parts += ["", f"> Attachment: {a.get('file_name', 'file')}", "", (a.get("extracted_content") or "")[:20000].strip()]
        names = [f.get("file_name") for f in (m.get("files") or []) if f.get("file_name")]
        if names:
            parts += ["", "> Files: " + ", ".join(names)]
        out += parts + [""]
    return "\n".join(out), title, created


def project_to_md(p):
    out = [f"# Claude project: {p.get('name', '(unnamed)')}", "", f"Date: {(p.get('created_at') or '')[:10]}", ""]
    if p.get("description"):
        out += ["## Description", "", p["description"], ""]
    if p.get("prompt_template"):
        out += ["## Project instructions", "", p["prompt_template"], ""]
    for d in p.get("docs") or []:
        out += [f"## Doc: {d.get('filename', 'doc')}", "", (d.get("content") or "")[:40000], ""]
    return "\n".join(out)


def ingest(zip_path=None, force=False):
    secure_dirs()
    st = load_state()
    parts = find_export(zip_path)
    if not parts:
        print(f"ingest: no Claude export (zip containing conversations.json) found in {DOWNLOADS}")
        return False
    sig = [export_name(parts), sum(p.stat().st_size for p in parts), int(max(p.stat().st_mtime for p in parts))]
    if st.get("export") == sig and not force:
        print(f"ingest: {sig[0]} already ingested ({len(st.get('docs', {}))} docs)")
        return False
    root = extract(parts)
    users = load_many(root, "users.json", "users")
    # The account display name may not be the person's real name, and models then treat the speaker
    # as a third party. JARVIS_USER_NAME wins.
    me = os.environ.get("JARVIS_USER_NAME") or (((users[0].get("full_name") or "").split() or ["Me"])[0] if users else "Me")
    convs = load_many(root, "conversations.json", "conversations")
    projects = load_many(root, "projects.json", "projects")
    memories = load_many(root, "memories.json", "memories") or None

    docs_before = st.get("docs", {})
    docs, totals, with_red = {}, Counter(), 0
    added = changed = 0

    def put(key, md, fname, meta):
        nonlocal added, changed, with_red
        clean, cnt = scrub(md)
        totals.update(cnt)
        with_red += 1 if cnt else 0
        h = sha(clean)
        prev = docs_before.get(key)
        if prev and prev["file"] != fname:
            (CONV / prev["file"]).unlink(missing_ok=True)
        if not prev or prev["sha"] != h or not (CONV / fname).exists():
            write_private(CONV / fname, clean)
            added += 0 if prev else 1
            changed += 1 if prev else 0
        docs[key] = {"file": fname, "sha": h, **meta}

    for c in convs:
        md, title, created = conv_to_md(c, me)
        key = c.get("uuid") or sha(md)[:16]
        put(key, md, f"{created or '0000-00-00'}_{slug(title)}_{key[:8]}.md",
            {"title": title, "date": created, "updated": (c.get("updated_at") or "")[:10],
             "starred": bool(c.get("is_starred")), "kind": "conversation"})
    for p in projects:
        key = "project-" + (p.get("uuid") or slug(p.get("name")))
        put(key, project_to_md(p), f"project_{slug(p.get('name'))}_{key[8:16]}.md",
            {"title": p.get("name"), "date": (p.get("created_at") or "")[:10], "kind": "project"})
    if memories:
        put("memories", "# Claude memories\n\n" + json.dumps(memories, indent=1, ensure_ascii=False),
            "claude-memories.md", {"title": "Claude memories", "date": "", "kind": "memories"})

    removed = 0
    for key, prev in docs_before.items():  # newest export is a full snapshot: mirror it
        if key not in docs:
            (CONV / prev["file"]).unlink(missing_ok=True)
            (SUMM / f"{key}.json").unlink(missing_ok=True)
            removed += 1
    st.update(export=sig, docs=docs, user_first_name=me, ingested_at=datetime.now().isoformat(timespec="seconds"))
    save_state(st)
    report = {"export": sig[0], "at": st["ingested_at"], "documents": len(docs),
              "redactions_total": sum(totals.values()), "documents_with_redactions": with_red,
              "by_type": dict(totals.most_common())}
    write_private(PK / "redaction-report.json", json.dumps(report, indent=1))
    shutil.rmtree(root, ignore_errors=True)  # raw export is unscrubbed: never keep it after converting
    print(f"ingest: {sig[0]} -> {len(docs)} docs (+{added} ~{changed} -{removed}) | "
          f"redacted {report['redactions_total']} secrets in {with_red} docs: {report['by_type']}")
    return True


# ---- LLM (local Ollama only) ------------------------------------------------------------
def llm(prompt, system="", json_mode=False, max_tokens=1200):
    body = {"model": MODEL, "stream": False, "think": False, "keep_alive": os.environ.get("OLLAMA_KEEP_ALIVE", "5m"),
            "options": {"num_ctx": 32768, "temperature": 0.2, "num_predict": max_tokens,
                        "repeat_penalty": 1.15, "repeat_last_n": 256},
            "messages": ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]}
    if json_mode:
        body["format"] = "json"
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=1800) as r:
        out = json.loads(r.read())["message"]["content"]
    return re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()


MAX_SUMMARY_PARTS = int(os.environ.get("JARVIS_MAX_SUMMARY_PARTS", "8"))


def prose(md):
    """Conversation text without tool calls/results and attachment bodies (the bulk, little signal)."""
    md = re.sub(r"\[tool (?:call|result)[^\n]*(?:\n(?!## )[^\n]*)*", "", md)
    md = re.sub(r"> Attachment:.*?(?=\n## |\Z)", "", md, flags=re.S)
    return re.sub(r"\n{3,}", "\n\n", md)


def sample(parts, k):
    if len(parts) <= k:
        return list(enumerate(parts))
    idx = sorted({round(i * (len(parts) - 1) / (k - 1)) for i in range(k)})  # evenly spaced, incl. first+last
    return [(i, parts[i]) for i in idx]


def chunks(text, size=CHUNK):
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def reduce_notes(items, instruction, size=CHUNK):
    """Compress a long list of text items into notes that fit one LLM pass."""
    blob = "\n\n".join(items)
    while len(blob) > size:
        parts, cur = [], ""
        for it in blob.split("\n\n"):
            if len(cur) + len(it) > size and cur:
                parts.append(cur)
                cur = ""
            cur += it + "\n\n"
        parts.append(cur)
        blob = "\n\n".join(llm(f"{instruction}\n\nCompress these notes, keeping every concrete fact, "
                               f"date, number, name and open item. Newest information wins on conflicts.\n\n{p}")
                           for p in parts)
    return blob


SUM_SYS = ("You summarize one of Rudra's past conversations with Claude for his private, local assistant. "
           "Be factual and specific (names, numbers, dates, decisions, outcomes). Never invent. "
           "STRICT: distinguish what Rudra DID or DECIDED from what was only suggested, planned or recommended. "
           "Advice from Claude is never an action or decision (write 'Claude advised X', or list it as an open item). "
           "Never upgrade 'planned' to 'done'. Never reproduce anything marked [REDACTED:...].")


# ---- 2. summaries --------------------------------------------------------------------
def summarize(force=False):
    st = load_state()
    docs = st.get("docs", {})
    todo = [(k, d) for k, d in docs.items() if d["kind"] != "memories"]
    done = skipped = 0
    t0 = time.time()
    for i, (key, d) in enumerate(sorted(todo, key=lambda kv: kv[1]["date"])):
        out = SUMM / f"{key}.json"
        if out.exists() and not force and json.loads(out.read_text()).get("sha") == d["sha"]:
            skipped += 1
            continue
        text = prose((CONV / d["file"]).read_text(encoding="utf-8"))
        parts = chunks(text)
        picked = sample(parts, MAX_SUMMARY_PARTS)
        coverage = f"{len(picked)}/{len(parts)} parts"
        if len(parts) > 1:
            notes = [f"[part {n + 1}/{len(parts)}] " + llm(f"Part {n + 1}/{len(parts)} of the conversation \"{d['title']}\". "
                     f"Write up to 150 words of factual notes.\n\n{p}", SUM_SYS, max_tokens=400)
                     for n, p in picked]
            material = ("NOTES FROM PARTS (later parts are newer; newest wins):\n\n" + "\n\n".join(notes))
        else:
            material = text
        raw = llm(f"Conversation \"{d['title']}\" dated {d['date']}.\n\nReturn JSON: "
                  '{"summary": "<3-6 lines: what it was about, decisions, numbers, outcome, open items>", '
                  f'"projects": [<one or more of {json.dumps(TAGS)}>]}}\n\n{material}',
                  SUM_SYS, json_mode=True, max_tokens=500)
        try:
            j = json.loads(raw)
        except json.JSONDecodeError:
            j = {"summary": raw[:800], "projects": ["Other"]}
        tags = [t for t in (j.get("projects") or []) if t in TAGS] or ["Other"]
        summary, _ = scrub(str(j.get("summary", "")).strip())
        write_private(out, json.dumps({"sha": d["sha"], "title": d["title"], "date": d["date"],
                                       "updated": d.get("updated", ""), "summary": summary, "projects": tags,
                                       "coverage": coverage}, indent=1))
        done += 1
        print(f"  summarized {i + 1}/{len(todo)}: {d['date']} {d['title'][:50]} -> {tags} ({coverage})", flush=True)
    print(f"summarize: {done} new/changed, {skipped} unchanged ({time.time() - t0:.0f}s)")
    return done


def load_summaries():
    out = []
    for f in SUMM.glob("*.json"):
        j = json.loads(f.read_text())
        j["key"] = f.stem
        out.append(j)
    return sorted(out, key=lambda j: (j.get("updated") or j["date"], j["date"]))


# ---- 3. profiles ---------------------------------------------------------------------
def gen_projects(summ):
    sections = []
    for proj in PROJECTS:
        items = [f"[chat started {s['date']}{', last active ' + s['updated'] if s.get('updated') and s['updated'] != s['date'] else ''}] "
                 f"{s['title']}: {s['summary']}" for s in summ if proj in s["projects"]]
        if not items:
            sections.append(f"## {proj}\n\n- No conversations tagged with this project.\n")
            continue
        notes = reduce_notes(items, f"These are dated summaries of Rudra's conversations about {proj}, oldest first.")
        body = llm(f"Below are dated summaries of Rudra's conversations that touch **{proj}**, oldest first. Many chats "
                   f"cover several topics: use ONLY facts that are specifically about {proj} and ignore everything else "
                   "(other businesses, relationships, school, other projects). "
                   "NEWEST INFORMATION WINS when they conflict (a later date overrides an earlier one). "
                   "Date each bullet with the chat's start date unless the summary gives a more specific date. "
                   "Write the markdown body of this project's section with exactly these bold labels, "
                   "each followed by tight bullets: **Current status**, **Key decisions**, **Numbers**, "
                   "**People** (other people only, never Rudra or Claude; write '- none named' if none), **Open items**. "
                   "Put a date like (2026-09-12) on each bullet. Anything Claude only recommended goes under Open items, "
                   "never under decisions or status. "
                   "Only facts present below. Mark anything uncertain with (uncertain). Max 350 words.\n\n" + notes,
                   SUM_SYS, max_tokens=1400)
        sections.append(f"## {proj}\n\n{body}\n")
    newest = max((s.get("updated") or s["date"] for s in summ if "empty" not in s["summary"].lower()[:200]), default="")
    focus = llm(f"From these project sections, write Rudra's focus AS OF HIS CLAUDE HISTORY (latest substantive chat "
                f"around {newest}): at most 200 words, 5-8 bullets, the OPEN items that were most recent and most "
                "important, newest first, each with its date. Skip anything already done. Only facts below.\n\n"
                + "\n".join(sections), SUM_SYS, max_tokens=600)
    focus = (f"_As of Claude history through {newest}. The PersonalBrain vault (Daily notes, Home) is newer; "
             f"when they differ, the vault wins._\n\n{focus}")
    return (f"# Projects (from Claude history)\n\nGenerated {datetime.now():%Y-%m-%d} by Jarvis from {len(summ)} "
            f"conversation summaries (newest {newest}). Newest information wins.\n\n## Current focus\n\n{focus}\n\n"
            + "\n".join(sections))


def find_core(st):
    docs = st.get("docs", {})
    norm = lambda s: re.sub(r"[^a-z]", "", (s or "").lower())  # noqa: E731
    target = norm(CORE_TITLE)
    if not target:
        return None
    # Exact match, or the same letters with a different number of repeats ("hmmm" vs "hmmmmm").
    cands = [(k, d) for k, d in docs.items() if d["kind"] == "conversation"
             and (norm(d["title"]) == target or (norm(d["title"]).startswith(target[:3]) and set(norm(d["title"])) <= set(target)))]
    if not cands:
        return None

    def score(kv):
        d = kv[1]
        try:
            days = abs((datetime.fromisoformat(d["date"]) - datetime.fromisoformat(CORE_DATE)).days)
        except ValueError:
            days = 999
        return (not d.get("starred"), days, -(CONV / d["file"]).stat().st_size)
    return sorted(cands, key=score)[0]


def gen_life_context(st):
    core = find_core(st)
    if not core:
        print("profiles: JARVIS_CORE_TITLE unset or not found; life-context.md skipped")
        return None, None
    key, d = core
    text = prose((CONV / d["file"]).read_text(encoding="utf-8"))  # full coverage for the core chat
    sysmsg = ("You extract facts about Rudra's personal life from his own private conversation, for his private, "
              "local assistant. Only what the conversation states, in close to his own words. Never invent, never "
              "diagnose, never embellish: do not characterize a relationship or feeling (e.g. 'strained', 'deep "
              "closeness', 'for a long time') unless he said it; if you must infer, append (uncertain). Do not pad. "
              "Never reproduce [REDACTED:...] values.")
    cache = PROF / ".cache-life-notes.json"  # per-part extraction is the slow step; reuse while the chat is unchanged
    cached = json.loads(cache.read_text()) if cache.exists() else {}
    if cached.get("sha") == d["sha"]:
        notes = cached["notes"]
    else:
        notes = [f"[part {n + 1}/{len(chunks(text))}] " + llm(
                     f"Part {n + 1}/{len(chunks(text))} of the conversation \"{d['title']}\" (started {d['date']}). "
                     "List facts under: People (who, relationship to Rudra), Relationships, Events (with dates), "
                     "Feelings, Decisions, Open threads. Bullets, max 300 words.\n\n" + p, sysmsg, max_tokens=700)
                 for n, p in enumerate(chunks(text))]
        write_private(cache, json.dumps({"sha": d["sha"], "notes": notes}))
    notes = reduce_notes(notes, "Facts extracted from one personal conversation, in order (later parts are newer).")
    rules = ("Write in second person ('you'). Only facts from the notes; keep every (uncertain) mark; add (uncertain) "
             "to anything ambiguous. Compassionate, plain, no therapy-speak. Bullets only, no intro or outro. This text "
             "is loaded into EVERY reply: summarize sexual or intimate details in one neutral, non-graphic line; omit "
             "contact details (emails, phone numbers, handles); keep appearance/self-image items to one line total.")
    plan = [("People", 260, "the people who matter, one bullet each: who they are to you, in a few words"),
            ("Relationships", 230, "where each important relationship stands now (newest state wins)"),
            ("Events", 380, "at most 14 bullets, dated, chronological; weight the later months more"),
            ("Feelings", 180, "the recurring feelings, with when they show up"),
            ("Decisions", 200, "decisions you made, dated"),
            ("Open threads", 220, "what is still unresolved as of the end of the chat")]
    sections = []
    for name, words, what in plan:
        txt = llm(f"From the notes below, write ONLY the '{name}' section of your life context: {what}. "
                  f"At most {words} words. {rules}\n\nNOTES:\n{notes}", sysmsg, max_tokens=int(words * 2.2))
        txt = re.sub(r"^#+ .*\n", "", txt.strip())
        if len(txt.split()) > words * 1.25 or not txt.rstrip().endswith((".", ")", '"', "'", "*")):
            txt = txt[: txt.rfind("\n")] if "\n" in txt else txt  # trim to the last complete bullet
        sections.append(f"## {name}\n\n{txt.strip()}\n")
    body = "\n".join(sections)
    head = (f"# Life context\n\nSource: the conversation \"{d['title']}\" ({d['date']}, file {d['file']}). "
            f"Generated {datetime.now():%Y-%m-%d}. Only what that chat says; (uncertain) marks inferred or unclear points.\n\n")
    return head + body, d["sha"]


PREF_RX = re.compile(r"(?i)\b(always|never|don'?t|do not|stop|please|i want|i prefer|i like|i hate|from now on|"
                     r"every (?:time|reply|message)|make sure|instead|too (?:long|short|wordy)|be (?:direct|honest|brief)|"
                     r"no fluff|verify|double[- ]check|remind me|keep it|you should|you need to|my rule|only (?:list|show|use))\b")
def me_seeds():
    """Rules stated directly by the user. Override with private-knowledge/profile/seeds.json (a JSON list)."""
    f = PROF / "seeds.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else DEFAULT_SEEDS


DEFAULT_SEEDS = [
    "Be direct, no fluff.", "Flag risks early.", "Show my full to-do list only when I say \"list\", \"todo\" or \"what's left\", or during /checkin; otherwise keep the ~150-word default.",
    "Verify 'shipped' claims before reporting them.", "Leave business, legal and pricing decisions to me.",
]


def gen_me(st):
    docs = st.get("docs", {})
    snippets = []
    for key, d in sorted(docs.items(), key=lambda kv: kv[1].get("updated") or kv[1]["date"], reverse=True):
        if d["kind"] != "conversation":
            continue
        text = (CONV / d["file"]).read_text(encoding="utf-8")
        for m in re.finditer(r"^## (?!Claude)[^\n]*·[^\n]*\n\n(.*?)(?=^## |\Z)", text, flags=re.S | re.M):
            msg = m.group(1).strip()
            if PREF_RX.search(msg):
                snippets.append(f"[{d['date']}] {msg[:700]}")
        if sum(map(len, snippets)) > 160_000:  # newest-first cap
            break
    cache = PROF / ".cache-me-notes.json"
    key = sha("".join(snippets))
    cached = json.loads(cache.read_text()) if cache.exists() else {}
    if cached.get("sha") == key:
        notes = cached["notes"]
    else:
        notes = reduce_notes(snippets or ["(no instruction-like messages found)"],
                             "Messages Rudra wrote to Claude (newest first). Extract only instructions/preferences "
                             "about HOW he wants to be helped (style, format, process, boundaries), with dates.")
        write_private(cache, json.dumps({"sha": key, "notes": notes}))
    body = llm("Write me.md: Rudra's operating manual for his PERSONAL ASSISTANT (a chat assistant for his life, "
               "school, and businesses), i.e. HOW he likes to be helped. Start with a '## Rules I stated directly' "
               "section listing every rule below verbatim. Then the other sections. Leave out workflow rules that "
               "only apply to coding agents (git commits, PRs, merging, credits, specific tools). Each rule appears "
               "ONCE; never repeat a bullet. "
               "(communication style, format, process, verification, boundaries). Include every rule he stated "
               "directly, plus preferences clearly evidenced in his messages. EXCLUDE: facts about his life, his "
               "tasks or to-dos, project details, and anything about how to write this file (these writing "
               "instructions are not his preferences). Group under ## headings: Communication, Honesty & risk, "
               "Task tracking, Verification, Decisions that are his, Formatting rules, Other (omit empty headings). "
               "Each bullet is an instruction to the assistant. When messages conflict, the newest wins. "
               "Append (uncertain) to weakly-evidenced items. Max 600 words, at most 45 bullets.\n\n"
               "RULES HE STATED DIRECTLY:\n- " + "\n- ".join(me_seeds()) + "\n\nEVIDENCE FROM HIS MESSAGES:\n" + notes,
               SUM_SYS, max_tokens=1400)
    # Filter pass: a 27B model mixes tasks/coding rules/facts into preferences; a second, narrow pass removes them.
    body = llm("Below is a draft operating manual. Return it with these bullets DELETED: (a) tasks or to-dos "
               "(things to build, buy, submit, set up); (b) workflow rules for coding agents (git, commits, PRs, "
               "branches, CLI commands, file writes, hooks, model/tool settings); (c) facts or decisions about a "
               "specific project, person, price or plan; (d) anything containing [REDACTED. Keep the "
               "'## Rules I stated directly' section exactly as is. Keep every other bullet VERBATIM; do not add, "
               "merge or reword. Drop headings left empty. Output only the markdown.\n\n" + body,
               SUM_SYS, max_tokens=1800)
    return (f"# How I want to be helped (me.md)\n\nGenerated {datetime.now():%Y-%m-%d} from {len(snippets)} of my "
            f"messages to Claude plus rules I stated directly. Newest preference wins.\n\n{body}"), sha(notes + "".join(me_seeds()))


GPA_RX = re.compile(r"(?i)(?:,?\s*(?:with|and|at)\s+)?(?:an?\s+|his\s+|your\s+)?(?:(?:projected|cumulative|term|current)\s+)?"
                    r"(?:GPA\b(?:\s+(?:of|is|was|at)\b|\s*:)?\s*(?P<a>\d\.\d{1,2})|(?P<b>\d\.\d{1,2})\+?\s*GPA\b)(?:\s+projected)?")


def apply_corrections(text):
    """profile/corrections.json: [{"find": regex, "replace": str, "why": str}] fixes verified against the
    source chats. Applied after every regeneration so hand corrections survive `jarvis sync`."""
    f = PROF / "corrections.json"
    for c in (json.loads(f.read_text()) if f.exists() else []):
        text = re.sub(c["find"], c["replace"], text)
    return text


def enforce_rules(text, gpa_min=None):
    """Code-level rules applied after every generation (not left to the model): hand corrections, duplicate
    bullets dropped, and (if JARVIS_GPA_MIN is set) GPA mentions at or below that value removed."""
    gpa_min = GPA_MIN if gpa_min is None else gpa_min
    text = apply_corrections(text)
    out, seen = [], set()
    for line in text.splitlines():
        key = re.sub(r"[\W_]+", " ", line).strip().lower()
        if line.lstrip().startswith(("-", "*")) and len(key) > 8:
            if key in seen:  # drop duplicate bullets (guards against generation loops)
                continue
            seen.add(key)
        def drop(m):
            return m.group(0) if float(m.group("a") or m.group("b")) > gpa_min else ""
        new = GPA_RX.sub(drop, line) if gpa_min is not None else line
        if new != line:
            body = re.sub(r"^[\s*\-]*(\(\d{4}-\d{2}-\d{2}\))?", "", new)
            if len(re.sub(r"[\W\d_]", "", body)) < 12:  # nothing meaningful left
                continue
            new = re.sub(r"([,;])\s*([,;.])", r"\2", re.sub(r"\s+([.,;])", r"\1", new))
        out.append(new)
    return "\n".join(out)


def private_terms():
    """Regex (one per line, '#' comments allowed) for details that must not appear verbatim in the always-on
    profile. Kept in the gitignored private-knowledge/profile/neutralize.txt; empty by default = no filtering."""
    f = PROF / "neutralize.txt"
    terms = [t.strip() for t in (f.read_text().splitlines() if f.exists() else []) if t.strip() and not t.lstrip().startswith("#")]
    return re.compile(r"(?i)\b(?:" + "|".join(terms) + r")\b") if terms else None


def neutralize_intimate(text):
    """life-context.md is loaded into every reply: explicit specifics become one neutral line (date kept)."""
    rx = private_terms()
    if rx is None:
        return text
    out = []
    for line in text.splitlines():
        if line.lstrip().startswith(("-", "*")) and rx.search(line):
            date = re.search(r"\d{4}-\d{2}-\d{2}", line)
            line = (f"*   Private (intimate detail){' ' + chr(40) + date.group(0) + chr(41) if date else ''}; "
                    "details stay in the source chat.")
        out.append(line)
    return "\n".join(out)


def profiles(force=False, which=("projects", "life-context", "me")):
    st = load_state()
    prof_state = st.setdefault("profiles", {})
    summ = load_summaries()
    jobs = {
        "projects": (lambda: (gen_projects(summ), None), sha(json.dumps([[s["sha"], s["summary"]] for s in summ]))),
        "life-context": (lambda: gen_life_context(st), (find_core(st) or (None, {"sha": None}))[1]["sha"]),
        "me": (lambda: gen_me(st), sha(json.dumps(sorted((k, d["sha"]) for k, d in st.get("docs", {}).items())))),
    }
    for name in which:
        fn, inputs = jobs[name]
        inputs = f"{inputs}|gen-v2" if name == "projects" else inputs  # generator revision
        path = PROF / f"{name}.md"
        if path.exists() and prof_state.get(name) == inputs and not force:
            print(f"profiles: {name}.md unchanged")
            continue
        t0 = time.time()
        text, _ = fn()
        if text is None:
            continue
        clean, cnt = scrub(enforce_rules(neutralize_intimate(text) if name == "life-context" else text))
        write_private(path, clean.strip() + "\n")
        prof_state[name] = inputs
        save_state(st)
        print(f"profiles: wrote {path.name} ({len(clean.split())} words, {time.time() - t0:.0f}s"
              f"{', scrubbed ' + str(sum(cnt.values())) if cnt else ''})")


def rescrub():
    """Re-apply the current scrubber to every derived file in place (after scrubber improvements).
    Redaction doesn't change meaning, so summaries are kept and their hashes updated, not regenerated."""
    st = load_state()
    total = Counter()
    for key, d in st.get("docs", {}).items():
        f = CONV / d["file"]
        old = f.read_text(encoding="utf-8")
        new, c = scrub(old)
        if c:
            total.update(c)
            write_private(f, new)
            d["sha"] = sha(new)
            sj = SUMM / f"{key}.json"
            if sj.exists():
                j = json.loads(sj.read_text())
                j["sha"], j["summary"] = d["sha"], scrub(j["summary"])[0]
                write_private(sj, json.dumps(j, indent=1))
    save_state(st)
    for f in list(SUMM.glob("*.json")) + list(PROF.glob("*.md")) + list(PROF.glob(".cache-*.json")):
        old = f.read_text(encoding="utf-8")
        new, c = scrub(old)
        if c:
            total.update(c)
            write_private(f, new)
    rep = PK / "redaction-report.json"
    if rep.exists():
        r = json.loads(rep.read_text())
        by = Counter(r["by_type"])
        by.update({k: v for k, v in total.items()})
        r["by_type"], r["redactions_total"] = dict(by.most_common()), sum(by.values())
        r["rescrubbed_at"] = datetime.now().isoformat(timespec="seconds")
        write_private(rep, json.dumps(r, indent=1))
    print(f"rescrub: {sum(total.values())} additional redactions {dict(total)}")


def status():
    st = load_state()
    rep = PK / "redaction-report.json"
    print(f"private-knowledge: {PK}")
    print(f"  export: {st.get('export')} | docs: {len(st.get('docs', {}))} | summaries: {len(list(SUMM.glob('*.json'))) if SUMM.exists() else 0}")
    print(f"  profiles: {[p.name for p in PROF.glob('*.md')] if PROF.exists() else []}")
    if rep.exists():
        r = json.loads(rep.read_text())
        print(f"  redactions: {r['redactions_total']} in {r['documents_with_redactions']} docs {r['by_type']}")
    parts = find_export()
    if parts and st.get("export", [None])[0] != export_name(parts):
        print(f"  NEW EXPORT AVAILABLE: {export_name(parts)} (run `jarvis sync`)")


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args and not args[0].startswith("--") else "status"
    force = "--force" in args
    zp = args[args.index("--zip") + 1] if "--zip" in args else None
    if cmd == "ingest":
        ingest(zp, force)
    elif cmd == "summarize":
        summarize(force)
    elif cmd == "profiles":
        only = [a for a in args[1:] if a in ("projects", "life-context", "me")]
        profiles(force, tuple(only) or ("projects", "life-context", "me"))
    elif cmd == "all":
        ingest(zp, force)
        if load_state().get("docs"):
            summarize(force)
            profiles(force)
    elif cmd == "status":
        status()
    elif cmd == "rescrub":
        rescrub()
    elif cmd == "reconvert":  # same export, new scrubber: rebuild markdown from raw, keep summaries (re-keyed)
        before = {k: d["sha"] for k, d in load_state().get("docs", {}).items()}
        ingest(zp, force=True)
        st = load_state()
        kept = 0
        for k, d in st.get("docs", {}).items():
            sj = SUMM / f"{k}.json"
            if sj.exists() and k in before:
                j = json.loads(sj.read_text())
                j["sha"], j["summary"] = d["sha"], scrub(j["summary"])[0]
                write_private(sj, json.dumps(j, indent=1))
                kept += 1
        print(f"reconvert: re-keyed {kept} summaries (content unchanged except redactions)")
    else:
        sys.exit(__doc__)
