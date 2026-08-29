from __future__ import annotations

import hashlib
import json
from pathlib import Path


def write_creative_approval(edit_dir: Path) -> None:
    """Record a minimal hash-bound Gate 3 approval for an already-approved fixture."""

    treatment = edit_dir / "creative_treatment_plan.json"
    treatment.write_text(
        json.dumps(
            {"version": 1, "type": "sprut_creative_treatment_plan"},
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    approval = {
        "version": 1,
        "type": "videomontazhka_creative_approval",
        "status": "approved",
        "creative_treatment_plan": treatment.name,
        "creative_treatment_plan_sha256": hashlib.sha256(treatment.read_bytes()).hexdigest(),
        "user_quote": "Approve this exact synthetic creative treatment fixture.",
    }
    (edit_dir / "creative_approval.json").write_text(
        json.dumps(approval, sort_keys=True) + "\n",
        encoding="utf-8",
    )
