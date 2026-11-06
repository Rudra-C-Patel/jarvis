import zipfile

import history as H


def test_slug():
    assert H.slug("Hello, World!") == "hello-world"
    assert H.slug("") == "untitled"


def test_sample_is_even_and_keeps_first_and_last():
    picked = [i for i, _ in H.sample(list(range(20)), 5)]
    assert picked[0] == 0 and picked[-1] == 19 and len(picked) == 5
    assert H.sample([1, 2], 5) == [(0, 1), (1, 2)]


def test_prose_strips_tool_noise():
    md = "## Me · t\n\nhi\n[tool call: x] {}\n[tool result] lots\n## Claude · t\n\nok"
    out = H.prose(md)
    assert "tool" not in out and "hi" in out and "ok" in out


def test_conv_to_md_labels_speaker():
    c = {"name": "T", "created_at": "2026-01-01T00:00", "uuid": "u1",
         "chat_messages": [{"sender": "human", "content": [{"type": "text", "text": "q"}]},
                           {"sender": "assistant", "content": [{"type": "text", "text": "a"}]}]}
    md, title, created = H.conv_to_md(c, "Alex")
    assert title == "T" and created == "2026-01-01"
    assert "## Alex ·" in md and "## Claude ·" in md


def test_extract_drops_path_traversal(tmp_path):
    z = tmp_path / "x.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../../escape.txt", "bad")
        zf.writestr("ok/conversations.json", "[]")
    dest = tmp_path / "out"
    H._extract_one(z, dest)
    assert (dest / "escape.txt").exists()  # '..' parts dropped, so it lands inside dest
    assert not (tmp_path.parent / "escape.txt").exists()
    assert (dest / "ok" / "conversations.json").exists()


def test_enforce_rules_dedupes_bullets(monkeypatch, tmp_path):
    monkeypatch.setattr(H, "PROF", tmp_path)
    out = H.enforce_rules("- Be direct always\n- Be direct always\n- Flag risks early", gpa_min=None)
    assert out.count("Be direct always") == 1


def test_gpa_filter_only_when_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(H, "PROF", tmp_path)
    monkeypatch.setattr(H, "GPA_MIN", None)
    line = "- Student at State U with a GPA of 3.5, studying CS"
    assert "3.5" in H.enforce_rules(line)
    assert "3.5" not in H.enforce_rules(line, gpa_min=3.8)
    assert "3.9" in H.enforce_rules("- Studying CS with a GPA of 3.9 overall", gpa_min=3.8)


def test_corrections_applied(monkeypatch, tmp_path):
    monkeypatch.setattr(H, "PROF", tmp_path)
    (tmp_path / "corrections.json").write_text('[{"find": "advised X", "replace": "advised Y", "why": "t"}]')
    assert H.enforce_rules("- Claude advised X", gpa_min=None) == "- Claude advised Y"


def test_neutralize_uses_local_term_file(monkeypatch, tmp_path):
    monkeypatch.setattr(H, "PROF", tmp_path)
    (tmp_path / "neutralize.txt").write_text("# local only\nzorbl\\w*\n")
    out = H.neutralize_intimate("- (2026-01-02) something zorbling happened\n- normal line")
    assert "zorbl" not in out and "(2026-01-02)" in out and "normal line" in out


def test_neutralize_is_noop_without_term_file(monkeypatch, tmp_path):
    monkeypatch.setattr(H, "PROF", tmp_path)
    text = "- (2026-01-02) anything at all\n- normal line"
    assert H.neutralize_intimate(text) == text


def test_find_core_matches_repeated_letters(monkeypatch, tmp_path):
    monkeypatch.setattr(H, "CORE_TITLE", "hmmm")
    monkeypatch.setattr(H, "CORE_DATE", "2026-01-01")
    monkeypatch.setattr(H, "CONV", tmp_path)
    (tmp_path / "a.md").write_text("x")
    st = {"docs": {"k": {"kind": "conversation", "title": "hmmmmmm", "date": "2026-01-01", "file": "a.md"}}}
    assert H.find_core(st)[0] == "k"
    monkeypatch.setattr(H, "CORE_TITLE", "")
    assert H.find_core(st) is None


def test_me_seeds_default_and_override(monkeypatch, tmp_path):
    monkeypatch.setattr(H, "PROF", tmp_path)
    assert H.me_seeds() == H.DEFAULT_SEEDS
    (tmp_path / "seeds.json").write_text('["only this"]')
    assert H.me_seeds() == ["only this"]
