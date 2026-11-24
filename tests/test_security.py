from __future__ import annotations

import pytest

from memdrift.security import (
    QUOTE_LIMIT,
    Redactor,
    is_private,
    redact_text,
    truncate_quote,
)


def test_windows_path_loses_the_account_name():
    raw = r"C:\Users\alice\vault\clients\acme.md"
    out = redact_text(raw)
    assert "alice" not in out
    assert out.endswith("clients/acme.md")


def test_root_prefix_becomes_tilde():
    out = redact_text("/home/alice/vault/clients/acme.md", root="/home/alice/vault")
    assert out == "~/clients/acme.md"


def test_posix_home_is_stripped_even_without_root():
    assert "alice" not in redact_text("/Users/alice/notes/a.md")


def test_plain_relative_path_is_untouched():
    assert redact_text("clients/acme.md") == "clients/acme.md"


def test_windows_path_embedded_in_text_is_redacted():
    """A path pasted mid-sentence, with a known vault root, keeps its shape."""
    root = r"C:\Users\alice\vault"
    out = redact_text(
        "Source: " + root + r"\clients\contract.pdf" + " (see attachment)",
        root=root,
    )
    assert out == "Source: ~/clients/contract.pdf (see attachment)"
    assert "alice" not in out


def test_truncated_preview_path_leaks_no_directories():
    """`differ.preview()` cuts Change values at 72 chars. The ellipsis means
    the path is incomplete — keeping its tail would expose `AppData` etc."""
    out = redact_text(
        "Notes. Source: C:/Users/admin/AppData/Loc…",
        root=r"C:\Users\admin\AppData\Local\Temp\vault",
    )
    assert "AppData" not in out
    assert "admin" not in out
    assert "…" in out


def test_short_absolute_path_loses_the_username():
    """Only two segments long: the username must still not survive."""
    out = redact_text(r"C:\Users\alice\x")
    assert "alice" not in out
    assert out == "~/x"


def test_redaction_is_idempotent():
    once = redact_text(r"C:\Users\alice\vault\a.md")
    assert redact_text(once) == once


def test_none_survives():
    assert redact_text(None) is None


def test_file_url_is_neutered():
    out = redact_text("file:///C:/Users/alice/vault/a.md")
    assert "alice" not in out


def test_truncate_quote_marks_the_cut():
    long = "x" * (QUOTE_LIMIT + 50)
    out = truncate_quote(long)
    assert len(out) <= QUOTE_LIMIT
    assert out.endswith("…")


def test_short_quote_is_verbatim():
    assert truncate_quote("we can do 48k") == "we can do 48k"


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"private": True}, True),
        ({"private": "yes"}, True),
        ({"confidential": True}, True),
        ({"private": False}, False),
        ({"private": "false"}, False),
        ({}, False),
    ],
)
def test_is_private(fields, expected):
    assert is_private(fields) is expected


def test_redactor_drops_quotes_when_asked():
    red = Redactor(root="/v", keep_quotes=False)
    out = red.evidence([{"kind": "email", "ref": "msg-1", "quote": "secret line"}])
    assert out[0]["quote_omitted"] is True
    assert "quote" not in out[0]


def test_redactor_keeps_truncated_quote_by_default():
    red = Redactor(root="/v")
    out = red.evidence([{"kind": "email", "ref": "msg-1", "quote": "y" * 400}])
    assert out[0]["quote"].endswith("…")
    assert len(out[0]["quote"]) <= QUOTE_LIMIT


def test_redactor_scrubs_change_paths_and_values():
    red = Redactor(root=r"C:\Users\alice\vault")
    clean = red.change({"path": "clients/acme.md", "op": "modify", "before": r"C:\Users\alice\x", "after": "ok"})
    assert "alice" not in clean["before"]
    assert clean["path"] == "clients/acme.md"


def test_redactor_returns_none_for_private_page_and_records_it():
    red = Redactor(root="/v")
    assert red.change({"path": "secret/diary.md"}, private=True) is None
    assert "secret/diary.md" in red.hidden_paths
