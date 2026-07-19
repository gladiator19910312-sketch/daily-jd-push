#!/usr/bin/env python3
"""Collect, validate, and dispatch a sanitized Agent Reach supplement."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
for import_path in (REPOSITORY_ROOT, SCRIPTS_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from radar_supplement import load_supplement  # noqa: E402
from collect_agent_reach import collect_agent_reach, _write_private  # noqa: E402


def workflow_command(
    encoded_supplement: str,
    *,
    workflow: str,
    ref: str,
    dry_run: bool,
    force_all: bool,
    dispatch_id: str,
) -> list[str]:
    return [
        "gh", "workflow", "run", workflow,
        "--ref", ref,
        "-f", f"dry_run={'true' if dry_run else 'false'}",
        "-f", f"force_all={'true' if force_all else 'false'}",
        "-f", f"agent_reach_supplement_b64={encoded_supplement}",
        "-f", f"dispatch_id={dispatch_id}",
    ]


def _run(args: list[str], timeout: int = 90) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"命令执行失败: {args[0]} {args[1]}") from exc


def _find_dispatched_run(
    workflow: str,
    dispatch_id: str,
    started_at: datetime,
) -> dict[str, Any] | None:
    expected_title = f"Agent Reach {dispatch_id}"
    for _ in range(6):
        result = _run(
            [
                "gh", "run", "list", "--workflow", workflow,
                "--event", "workflow_dispatch", "--limit", "3",
                "--json", "databaseId,status,conclusion,url,createdAt,displayTitle",
            ]
        )
        if result.returncode == 0:
            try:
                rows = json.loads(result.stdout)
            except json.JSONDecodeError:
                rows = []
            for row in rows if isinstance(rows, list) else []:
                try:
                    created = datetime.fromisoformat(
                        str(row.get("createdAt", "")).replace("Z", "+00:00")
                    )
                except ValueError:
                    continue
                if (
                    row.get("displayTitle") == expected_title
                    and created >= started_at - timedelta(seconds=10)
                ):
                    return row
        time.sleep(5)
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true", help="trigger GitHub Actions")
    parser.add_argument("--wait", action="store_true", help="wait for the dispatched run")
    parser.add_argument("--dry-run-workflow", action="store_true")
    parser.add_argument("--force-all", action="store_true")
    parser.add_argument("--workflow", default="daily-job-radar.yml")
    parser.add_argument("--ref", default="main")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = collect_agent_reach()
    with tempfile.TemporaryDirectory(prefix="agent-reach-radar-") as directory:
        supplement_path = Path(directory) / "supplement.json"
        _write_private(supplement_path, payload)
        bundle = load_supplement(supplement_path)
        social_ok = {
            row.channel for row in bundle.coverage
            if row.channel in {"xiaohongshu", "wechat"}
            and row.status in {"ok", "partial"}
        }
        if not social_ok or not bundle.signals:
            print("本机社交源没有生成可用证据；未触发工作流。", file=sys.stderr)
            return 2
        encoded = base64.b64encode(supplement_path.read_bytes()).decode("ascii")

    coverage_text = "，".join(
        f"{row.channel}:{row.status}/{row.relevant_count}/{row.detail_reads}"
        for row in bundle.coverage
    )
    if not args.send:
        print(f"补充包校验通过（{coverage_text}）；未触发工作流。")
        return 0

    started_at = datetime.now(timezone.utc)
    dispatch_id = f"ar-{started_at:%Y%m%d%H%M%S}-{uuid.uuid4().hex[:10]}"
    result = _run(
        workflow_command(
            encoded,
            workflow=args.workflow,
            ref=args.ref,
            dry_run=args.dry_run_workflow,
            force_all=args.force_all,
            dispatch_id=dispatch_id,
        )
    )
    if result.returncode != 0:
        print("GitHub workflow_dispatch 触发失败；补充包内容未输出。", file=sys.stderr)
        return 2
    print(f"已触发脱敏 Agent Reach 推送（{coverage_text}）。")
    if not args.wait:
        return 0

    run = _find_dispatched_run(args.workflow, dispatch_id, started_at)
    if not run:
        print("已触发，但未能定位对应的 GitHub Actions run。", file=sys.stderr)
        return 2
    run_id = str(run.get("databaseId"))
    watched = _run(["gh", "run", "watch", run_id, "--exit-status", "--interval", "10"], 1900)
    if watched.returncode != 0:
        print(f"GitHub Actions 执行失败：{run.get('url', '')}", file=sys.stderr)
        return 2
    print(f"GitHub Actions 执行成功：{run.get('url', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
