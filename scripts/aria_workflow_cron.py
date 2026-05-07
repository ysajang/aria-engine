#!/usr/bin/env python3
"""ARIA Engine - Workflow Cron Trigger

cron에서 호출하여 워크플로우를 HTTP API로 실행
ARIA 서버가 실행 중이어야 함

사용법:
    python scripts/aria_workflow_cron.py --workflow competitor-monitor
    python scripts/aria_workflow_cron.py --workflow kpi-briefing
    python scripts/aria_workflow_cron.py --workflow viral-analysis
    python scripts/aria_workflow_cron.py --list

crontab 예시 (KST = UTC-9이므로 UTC 기준으로 설정):
    # 경쟁사 모니터링: 매일 KST 08:00 = UTC 23:00 (전날)
    0 23 * * * /path/to/python scripts/aria_workflow_cron.py --workflow competitor-monitor

    # 주간 KPI 브리핑: 매주 월요일 KST 09:00 = UTC 00:00
    0 0 * * 1 /path/to/python scripts/aria_workflow_cron.py --workflow kpi-briefing

    # 바이럴 분석: 매주 월요일 KST 09:30 = UTC 00:30
    30 0 * * 1 /path/to/python scripts/aria_workflow_cron.py --workflow viral-analysis

    # 세금 자료: 매분기 말 KST 10:00 (3/31, 6/30, 9/30, 12/31)
    0 1 31 3,6,9,12 * /path/to/python scripts/aria_workflow_cron.py --workflow tax-summary
"""

import argparse
import json
import os
import sys
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


def main() -> None:
    parser = argparse.ArgumentParser(description="ARIA Workflow Cron Trigger")
    parser.add_argument("--workflow", type=str, help="실행할 워크플로우 ID")
    parser.add_argument("--list", action="store_true", help="등록된 워크플로우 목록")
    parser.add_argument(
        "--base-url",
        type=str,
        default=os.environ.get("ARIA_BASE_URL", "http://localhost:8100"),
        help="ARIA 서버 URL",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=os.environ.get("ARIA_API_KEY", ""),
        help="ARIA API 키",
    )
    parser.add_argument("--data", type=str, default="{}", help="초기 데이터 (JSON)")

    args = parser.parse_args()

    headers = {"Content-Type": "application/json"}
    if args.api_key:
        headers["X-API-Key"] = args.api_key

    if args.list:
        _list_workflows(args.base_url, headers)
    elif args.workflow:
        _execute_workflow(args.base_url, headers, args.workflow, args.data)
    else:
        parser.print_help()
        sys.exit(1)


def _list_workflows(base_url: str, headers: dict) -> None:
    """워크플로우 목록 조회"""
    url = f"{base_url}/v1/workflows"
    try:
        req = Request(url, headers=headers, method="GET")
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            workflows = data.get("workflows", [])
            print(f"등록된 워크플로우: {len(workflows)}개\n")
            for w in workflows:
                print(f"  [{w.get('category', '')}] {w['workflow_id']} — {w.get('description', '')}")
    except (URLError, HTTPError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def _execute_workflow(base_url: str, headers: dict, workflow_id: str, data_json: str) -> None:
    """워크플로우 실행"""
    url = f"{base_url}/v1/workflows/{workflow_id}/execute"

    try:
        initial_data = json.loads(data_json) if data_json != "{}" else {}
    except json.JSONDecodeError:
        print(f"ERROR: 유효하지 않은 JSON: {data_json}", file=sys.stderr)
        sys.exit(1)

    payload = json.dumps({"initial_data": initial_data}).encode("utf-8")

    try:
        req = Request(url, data=payload, headers=headers, method="POST")
        with urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())

            status = result.get("status", "unknown")
            steps = f"{result.get('steps_completed', 0)}/{result.get('steps_total', 0)}"
            ms = result.get("duration_ms", 0)

            if status == "completed":
                print(f"✅ {workflow_id} 완료 ({steps} 스텝 / {ms:.0f}ms)")
            elif status == "partial":
                print(f"⚠️ {workflow_id} 부분 완료 ({steps} 스텝 / {ms:.0f}ms)")
            else:
                error = result.get("error", "")
                print(f"❌ {workflow_id} 실패: {error[:300]}", file=sys.stderr)
                sys.exit(1)

            # 리포트 출력 (있으면)
            report = result.get("report", "")
            if report:
                print(f"\n{report}")

    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        print(f"ERROR HTTP {e.code}: {body}", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print(f"ERROR: ARIA 서버 연결 실패 ({base_url}): {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
