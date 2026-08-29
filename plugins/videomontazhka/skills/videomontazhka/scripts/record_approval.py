#!/usr/bin/env python3
"""Bind explicit user approval to the exact semantic-plan bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from schema_check import SchemaDefinitionError, Validator


REQUIRED_SCOPES = ["semantic_structure", "editing_strategy", "visual_strategy"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Record explicit approval of one semantic plan")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--quote", required=True, help="exact user approval text")
    parser.add_argument("--message-ref", help="optional task/message reference")
    parser.add_argument("--replace", action="store_true", help="replace a stale approval file")
    args = parser.parse_args()

    plan_path = args.plan.expanduser().resolve()
    if not plan_path.is_file():
        raise ValueError(f"semantic plan not found: {plan_path}")
    raw = plan_path.read_bytes()
    plan = json.loads(raw)
    if not isinstance(plan, dict):
        raise ValueError("semantic plan root must be an object")
    quote = args.quote.strip()
    if len(quote) < 2:
        raise ValueError("approval quote is empty")
    if plan.get("status") not in ("pending", "approved"):
        raise ValueError("semantic plan status must be pending or approved")
    schema_path = Path(__file__).resolve().parent.parent / "assets/semantic-plan.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    try:
        schema_errors = Validator(schema).validate(plan)
    except SchemaDefinitionError as exc:
        raise ValueError(f"semantic plan schema is invalid: {exc}") from exc
    if schema_errors:
        details = "; ".join(item.render() for item in schema_errors[:8])
        raise ValueError(f"semantic plan does not satisfy its schema: {details}")

    output = plan_path.parent / "approval.json"
    if output.exists() and not args.replace:
        existing = json.loads(output.read_text(encoding="utf-8"))
        current_hash = hashlib.sha256(raw).hexdigest()
        if existing.get("proposal_sha256") == current_hash:
            print(f"approval already matches: {output}")
            return 0
        raise ValueError("a different approval exists; use --replace only after new explicit approval")

    approval = {
        "version": 1,
        "proposal_file": plan_path.name,
        "proposal_id": plan.get("id", "semantic-plan"),
        "proposal_sha256": hashlib.sha256(raw).hexdigest(),
        "status": "approved",
        "approved_scope": REQUIRED_SCOPES,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "user_quote": quote,
        "user_message_ref": args.message_ref,
    }
    output.write_text(json.dumps(approval, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"approval recorded: {output}")
    print(f"plan sha256: {approval['proposal_sha256']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"record_approval: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
