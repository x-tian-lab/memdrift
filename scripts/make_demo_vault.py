"""Rebuild a realistic demo vault for screenshots.

Run from anywhere: python scripts/make_demo_vault.py [target_dir]

Simulates the exact story from the PRD: an agent runs a nightly consolidation,
changes a client quote, flips a status, creates a page, merges two contacts —
then we capture what memdrift says about it.
"""

from __future__ import annotations

import os
import shutil
import sys
import textwrap

BASE_FILES = {
    "clients/acme.md": """\
        ---
        client: Acme Corp
        pricing: "$45k"
        seats: 200
        status: active
        owner: dana
        ---
        # Acme

        ## Pricing

        45k 一年，200 个席位，年付。

        ## Notes

        2026 Q4 续约。Dana 是主要联系人，财务走 finance@acme.test。
        """,
    "clients/globex.md": """\
        ---
        client: Globex
        pricing: "$30k"
        status: active
        ---
        # Globex

        ## Notes

        试用期到 12 月。
        """,
    "topics/pricing.md": """\
        # 定价策略

        ## Tiers

        - Starter: 15k / 50 seats
        - Growth: 30k / 200 seats
        - Enterprise: 定制

        ## Discounts

        年付 9 折。
        """,
    "contacts/dana-lee.md": """\
        ---
        name: Dana Lee
        role: VP Ops
        company: Acme
        ---
        # Dana Lee

        ## 偏好

        周五交付。喜欢一页纸总结，不要长文档。
        """,
    "sessions/weekly.md": """\
        # 周会纪要 11/21

        ## 议题

        1. Acme 续约报价
        2. Globex 试用转正

        ## 决定

        Acme 可以让到 48k，条件是 200 席位不变。
        """,
}

ONBOARDING = """\
    # 新客户 Onboarding

    ## 流程

    1. 签合同
    2. 开通席位
    3. kickoff 会

    ## 负责人

    Dana 对接运营。
    """


def write(path: str, body: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(textwrap.dedent(body))


def build(root: str) -> None:
    if os.path.exists(root):
        shutil.rmtree(root)
    for rel, body in BASE_FILES.items():
        write(os.path.join(root, rel.replace("/", os.sep)), body)


def apply_nightly_agent_run(root: str) -> None:
    """What the agent does at 00:00 — the story the digest has to explain."""
    acme = os.path.join(root, "clients", "acme.md")
    text = open(acme, encoding="utf-8").read()
    text = text.replace('pricing: "$45k"', 'pricing: "$48k"')
    text = text.replace("2026 Q4 续约。", "2026 Q4 续约，已确认 48k。")
    open(acme, "w", encoding="utf-8", newline="\n").write(text)

    globex = os.path.join(root, "clients", "globex.md")
    text = open(globex, encoding="utf-8").read().replace("status: active", "status: on-hold")
    open(globex, "w", encoding="utf-8", newline="\n").write(text)

    write(os.path.join(root, "topics", "onboarding.md"), ONBOARDING)

    os.rename(
        os.path.join(root, "contacts", "dana-lee.md"),
        os.path.join(root, "contacts", "dana.md"),
    )


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "demo-vault"
    build(os.path.abspath(target))
    print(f"vault ready: {os.path.abspath(target)}")
