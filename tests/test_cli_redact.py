"""The digest's safety switch, tested on a real modify event.

An `add` commit carries no before/after preview, so it cannot prove anything
about path redaction. This test creates a change whose value contains an
absolute path and checks both sides of `--no-redact`.
"""

from __future__ import annotations

from click.testing import CliRunner

from memdrift.cli import main


def _vault_with_path(vault, tmp_path):
    CliRunner().invoke(main, ["init", str(vault)])
    page = vault / "clients" / "acme.md"
    machine_path = str(vault / "clients" / "contract.pdf")
    page.write_text(
        page.read_text(encoding="utf-8") + f"\nSource: {machine_path}\n",
        encoding="utf-8",
    )
    CliRunner().invoke(
        main,
        ["-C", str(vault), "commit", "-m", "note", "--evidence", f"file:{machine_path}"],
    )
    return machine_path


def test_digest_redacts_by_default(vault, tmp_path):
    machine_path = _vault_with_path(vault, tmp_path)
    out = tmp_path / "safe.html"
    result = CliRunner().invoke(main, ["-C", str(vault), "digest", "-o", str(out)])
    assert result.exit_code == 0, result.output
    html = out.read_text(encoding="utf-8")
    assert machine_path not in html
    assert "AppData" not in html
    assert "~/" in html


def test_no_redact_keeps_paths_and_warns(vault, tmp_path):
    machine_path = _vault_with_path(vault, tmp_path)
    out = tmp_path / "raw.html"
    result = CliRunner().invoke(
        main, ["-C", str(vault), "digest", "-o", str(out), "--no-redact"]
    )
    assert result.exit_code == 0, result.output
    html = out.read_text(encoding="utf-8")
    assert machine_path in html
    assert "warning" in result.output.lower()
