from __future__ import annotations

import re

from memdrift import digest, events
from memdrift import operations as ops


def _render(repo, **kwargs):
    stream = events.build(repo)
    return digest.render_digest(stream, **kwargs)


def test_html_is_self_contained(repo):
    """No network, ever: a digest gets mailed to people who trust you."""
    html = _render(repo)
    assert "http://" not in html
    assert "https://" not in html
    assert "@import" not in html
    assert "<img" not in html
    assert 'src="' not in html


def test_no_absolute_paths(repo):
    html = _render(repo)
    assert str(repo.root) not in html


def test_commit_message_is_redacted_too(repo, vault):
    """The digest is the file you hand to a client. So is the message in it.

    A message is free text an agent wrote; agents paste paths into messages,
    and a message is rendered at the top of every commit card.
    """
    (vault / "clients" / "acme.md").write_text(
        (vault / "clients" / "acme.md").read_text(encoding="utf-8") + "\n", encoding="utf-8"
    )
    ops.commit(repo, r"extracted from C:\Users\alice\vault\clients\acme.md")

    html = _render(repo)
    assert "alice" not in html
    assert "C:/Users" not in html and "C:\\Users" not in html
    assert "extracted from" in html  # redacted, not dropped


def test_shows_field_level_change(repo, vault):
    text = (vault / "clients" / "acme.md").read_text(encoding="utf-8").replace("$45k", "$48k")
    (vault / "clients" / "acme.md").write_text(text, encoding="utf-8")
    ops.commit(repo, "nightly")

    html = _render(repo)
    assert "frontmatter.pricing" in html
    assert "$45k" in html and "$48k" in html
    assert "nightly" in html


def test_private_page_never_renders(repo, vault):
    (vault / "secret").mkdir()
    (vault / "secret" / "diary.md").write_text("---\nprivate: true\n---\n# D\n\nvery private\n", encoding="utf-8")
    ops.commit(repo, "add diary")

    html = _render(repo)
    assert "secret/diary.md" not in html
    assert "very private" not in html
    assert "private: true" in html  # but it says that something was hidden


def test_quote_truncation_applies_in_html(repo, vault):
    long_quote = "q" * 500
    (vault / "clients" / "acme.md").write_text((vault / "clients" / "acme.md").read_text(encoding="utf-8") + "\n", encoding="utf-8")
    ops.commit(repo, "with quote", evidence=["email:msg-1"])

    stream = events.build(repo)
    stream.events[0]["evidence"] = [{"kind": "email", "ref": "msg-1", "quote": long_quote[:120] + "…"}]
    html = digest.render_digest(stream)
    assert long_quote not in html


def test_html_escapes_untrusted_text(repo, vault):
    (vault / "xss.md").write_text("# <script>alert(1)</script>\n\nbody\n", encoding="utf-8")
    ops.commit(repo, "<img src=x onerror=alert(1)>")
    html = _render(repo)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_empty_stream_still_renders(repo):
    html = _render(repo)
    assert "<!DOCTYPE html>" in html
    assert "Pages tracked" in html


def test_write_digest_creates_parent_dirs(repo, tmp_path):
    stream = events.build(repo)
    target = tmp_path / "nested" / "deeper" / "out.html"
    digest.write_digest(stream, target)
    assert target.exists()
    assert target.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


def test_default_filename_is_date_stamped(repo):
    assert re.match(r"memdrift-digest-\d{4}-\d{2}-\d{2}\.html", digest.default_filename())
