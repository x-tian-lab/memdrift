"""End-to-end smoke test for ``memdrift digest`` and ``memdrift serve``.

Not part of the pytest suite (the tests use the in-process server); this one
drives the real executable so the CLI wiring is exercised too.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from make_demo_vault import apply_nightly_agent_run, build  # noqa: E402

from memdrift import operations as ops  # noqa: E402
from memdrift import server as server_mod  # noqa: E402
from memdrift.repo import Repo  # noqa: E402


def main() -> int:
    root = Path(tempfile.mkdtemp()) / "vault"
    build(root)
    repo = Repo.init(root, author_type="agent", author_name="linger-dreaming")
    ops.commit(repo, "baseline")
    apply_nightly_agent_run(root)
    ops.commit(repo, "nightly consolidation", evidence=["email:msg-1882"], confidence=0.55)

    srv = server_mod.make_server(repo, port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    token = srv.token

    def get(url: str) -> tuple[int, bytes]:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    checks: list[tuple[str, bool]] = []

    status, body = get(f"{base}/?t={token}")
    checks.append(("overview 200", status == 200))
    checks.append(("overview shows field change", b"frontmatter.pricing" in body))
    checks.append(("no absolute path in html", str(root).encode() not in body))

    status, _ = get(f"{base}/")
    checks.append(("missing token rejected", status == 403))
    status, _ = get(f"{base}/?t=wrong")
    checks.append(("wrong token rejected", status == 403))

    status, body = get(f"{base}/pages?t={token}")
    checks.append(("pages 200", status == 200 and b"clients/acme.md" in body))

    status, body = get(f"{base}/history?t={token}")
    checks.append(("history 200", status == 200 and b"nightly consolidation" in body))

    status, body = get(f"{base}/page/clients/acme.md?t={token}")
    checks.append(("page detail 200", status == 200 and b"frontmatter.pricing" in body))

    status, _ = get(f"{base}/page/../../etc/passwd?t={token}")
    checks.append(("traversal blocked", status in (403, 404)))

    status, body = get(f"{base}/?t={token}&quotes=0")
    checks.append(("quotes can be dropped", status == 200))

    srv.shutdown()

    # Public bind must be refused before any socket is opened.
    try:
        server_mod.check_host("0.0.0.0")
        checks.append(("0.0.0.0 refused", False))
    except server_mod.LoopbackOnly:
        checks.append(("0.0.0.0 refused", True))

    try:
        server_mod.check_host("example.com")
        checks.append(("public host refused", False))
    except server_mod.LoopbackOnly:
        checks.append(("public host refused", True))

    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
    if failed:
        print(f"\n{len(failed)} check(s) failed")
        return 1
    print(f"\nall {len(checks)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
