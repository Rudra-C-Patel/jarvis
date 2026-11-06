from datetime import date

import sync


def test_clean_drops_obsidian_query_blocks():
    text = "# Note\n\n```dataview\nTABLE x\n```\n\n\n\nbody"
    assert sync.clean(text) == "# Note\n\nbody"


def test_priority_files_picks_home_personal_and_newest_dailies(monkeypatch):
    monkeypatch.setattr(sync, "RECENT_DAILY", 2)
    notes = ["Home.md", "Areas/Personal/Health.md", "Areas/Work/x.md",
             "Daily/2026-01-01.md", "Daily/2026-01-03.md", "Daily/2026-01-02.md"]
    assert sync.priority_files(notes) == ["Home.md", "Areas/Personal/Health.md",
                                          "Daily/2026-01-03.md", "Daily/2026-01-02.md"]


def test_current_focus_extracts_section():
    md = "# P\n\n## Current focus\n\n- ship it\n\n## Other\n\n- no"
    assert sync.current_focus(md) == "- ship it"
    assert sync.current_focus("# nothing here") == ""


def test_split_parts_splits_at_message_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "PART_BYTES", 50)
    f = tmp_path / "chat.md"
    f.write_text("# Title\nDate: 2026-01-01\n\n## A\n" + "x" * 40 + "\n## B\n" + "y" * 40 + "\n")
    parts = sync.split_parts(f)
    assert len(parts) == 2
    for body in parts.values():
        assert body.startswith(b"# Title")  # header repeated on every part
    assert all("of02" in k for k in parts)


def _vault(tmp_path, monkeypatch, files):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    monkeypatch.setattr(sync, "VAULT", tmp_path)
    return sorted(files)


def test_dated_entries_newest_first_and_skip_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "TODAY", date(2026, 11, 5))
    notes = _vault(tmp_path, monkeypatch, {
        "Projects/a.md": "# A\n- 2026-10-01 old fact\n- 2026-11-01 new fact\n- ~~2026-11-02 struck~~\n"
                         "- [ ] 2026-11-03 open task\n- 2026-12-01 future\n",
        "Daily/2026-11-04.md": "- 2026-11-04 daily is skipped\n",
    })
    got = [(d, t) for d, _, t in sync.dated_entries(notes)]
    assert got == [("2026-11-01", "2026-11-01 new fact"), ("2026-10-01", "2026-10-01 old fact")]


def test_bullets_inherit_heading_date(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "TODAY", date(2026, 11, 5))
    notes = _vault(tmp_path, monkeypatch, {"Meetings.md": "# Log\n### 2026-10-20 call\n- agreed on price\n"})
    assert sync.dated_entries(notes)[0][0] == "2026-10-20"


def test_recency_block_flags_stale_daily(tmp_path, monkeypatch):
    notes = _vault(tmp_path, monkeypatch, {"Daily/2026-10-01.md": "x"})
    monkeypatch.setattr(sync, "TODAY", date(2026, 10, 20))
    assert "STALE" in sync.recency_block(notes)
    monkeypatch.setattr(sync, "TODAY", date(2026, 10, 3))
    assert "STALE" not in sync.recency_block(notes)


def test_system_prompt_respects_budget_by_dropping_oldest_daily(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "TODAY", date(2026, 1, 4))
    monkeypatch.setattr(sync, "PK", tmp_path / "no-pk")
    prompt_file = tmp_path / "system_prompt.md"
    prompt_file.write_text("You are a test.")
    monkeypatch.setattr(sync, "PROMPT_FILE", prompt_file)
    notes = _vault(tmp_path / "v", monkeypatch, {f"Daily/2026-01-0{i}.md": "word " * 400 for i in (1, 2, 3)})
    monkeypatch.setattr(sync, "ALWAYS_ON_BUDGET", 900)
    prompt = sync.build_system_prompt(notes)
    assert "Daily/2026-01-03.md" in prompt  # newest kept
    assert "SOURCE: Daily/2026-01-01.md" not in prompt  # oldest dropped first
    assert sync.LAST_PROMPT_INFO["inlined"] < 3


def test_vault_walk_skips_hidden_and_excluded(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch, {"a.md": "x", ".obsidian/b.md": "x", "Templates/t.md": "x",
                                   "sub/c.md": "x", "sub/d.txt": "x"})
    assert sync._walk(tmp_path, "") == ["a.md", "sub/c.md"]
