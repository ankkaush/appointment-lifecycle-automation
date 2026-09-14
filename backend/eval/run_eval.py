"""Golden-set evaluation for the AI interpretation layer.

Not part of `pytest` — this calls the real Anthropic API, costs real
tokens, and has some inherent run-to-run variance the way any LLM does.
Run it manually, or from a CI job gated on the ANTHROPIC_API_KEY secret
being present:

    cd backend && python -m eval.run_eval

Metrics reported, and why each one earns its place:

  intent accuracy       -- wrong intent sends the whole downstream
                            workflow down the wrong path.
  ambiguity accuracy     -- did the model correctly flag cases it should
                            *not* resolve confidently, and correctly leave
                            clear cases alone? Recall on the "should have
                            been flagged" cases is reported separately —
                            it's the more dangerous failure mode of the
                            two, since a missed ambiguity flag is what lets
                            a bad interpretation slip through to booking.
  service-hint accuracy  -- only graded on cases that specify it.
  false-confidence rate  -- cases where the model was wrong AND reported
                            confidence >= 0.7. This is the sharpest
                            diagnostic: confidence is advisory-only in this
                            system's design (never a booking gate), but a
                            model that's both wrong and sure of itself is
                            a prompt/model quality problem worth tracking.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from app.ai.providers.claude import ClaudeInterpreter
from app.core.config import get_settings

GOLDEN_SET_PATH = Path(__file__).parent / "golden_set.yaml"
REPORT_PATH = Path(__file__).parent / "last_report.json"
TODAY = date(2026, 9, 14)  # fixed, so relative-date cases stay reproducible
CONFIDENCE_FALSE_POSITIVE_THRESHOLD = 0.7
INTENT_ACCURACY_FLOOR = 0.75
AMBIGUITY_RECALL_FLOOR = 0.7

# Rough, illustrative-only per-token rates (Haiku-tier) for a ballpark cost
# estimate in the printed report. Not a source of truth for billing --
# check the Anthropic console for exact usage/spend.
EST_USD_PER_INPUT_TOKEN = 1.0 / 1_000_000
EST_USD_PER_OUTPUT_TOKEN = 5.0 / 1_000_000


@dataclass
class CaseResult:
    id: str
    message: str
    ok: bool
    checks: dict[str, bool] = field(default_factory=dict)
    confidence: float = 0.0
    predicted_intent: str = ""
    predicted_is_ambiguous: bool = False
    expected_is_ambiguous: bool | None = None
    notes: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


async def _run_case(
    interpreter: ClaudeInterpreter, case: dict, default_services: list[str]
) -> CaseResult:
    expected = case["expected"]
    known_services = case.get("known_services", default_services)

    outcome = await interpreter.interpret(
        case["message"], today=TODAY, known_services=known_services
    )
    req = outcome.request

    checks: dict[str, bool] = {}
    if "intent" in expected:
        checks["intent"] = req.intent.value == expected["intent"]
    if "is_ambiguous" in expected:
        checks["is_ambiguous"] = req.is_ambiguous == expected["is_ambiguous"]
    if "service_hint_contains" in expected:
        checks["service_hint"] = bool(req.service_hint) and (
            expected["service_hint_contains"].lower() in req.service_hint.lower()
        )

    return CaseResult(
        id=case["id"],
        message=case["message"],
        ok=all(checks.values()),
        checks=checks,
        confidence=req.confidence,
        predicted_intent=req.intent.value,
        predicted_is_ambiguous=req.is_ambiguous,
        expected_is_ambiguous=expected.get("is_ambiguous"),
        notes=[f"service_hint={req.service_hint!r}"] if "service_hint_contains" in expected else [],
        input_tokens=outcome.metadata.input_tokens,
        output_tokens=outcome.metadata.output_tokens,
    )


async def run() -> int:
    settings = get_settings()
    if not settings.anthropic_api_key:
        print("ANTHROPIC_API_KEY is not set -- cannot run the golden-set eval.", file=sys.stderr)
        return 1

    data = yaml.safe_load(GOLDEN_SET_PATH.read_text())
    default_services = data.get("known_services_default", [])
    interpreter = ClaudeInterpreter(api_key=settings.anthropic_api_key, model=settings.ai_model)

    results = [await _run_case(interpreter, case, default_services) for case in data["cases"]]

    total = len(results)
    intent_checked = [r for r in results if "intent" in r.checks]
    intent_acc = sum(r.checks["intent"] for r in intent_checked) / len(intent_checked)

    ambiguous_expected = [r for r in results if r.expected_is_ambiguous is True]
    ambiguity_recall = (
        sum(r.predicted_is_ambiguous for r in ambiguous_expected) / len(ambiguous_expected)
        if ambiguous_expected
        else 1.0
    )

    service_checked = [r for r in results if "service_hint" in r.checks]
    service_acc = (
        sum(r.checks["service_hint"] for r in service_checked) / len(service_checked)
        if service_checked
        else 1.0
    )

    false_confident = [
        r for r in results if not r.ok and r.confidence >= CONFIDENCE_FALSE_POSITIVE_THRESHOLD
    ]

    print(f"\nGolden-set evaluation -- {total} cases\n")
    print(f"  intent accuracy:              {intent_acc:.0%}  ({len(intent_checked)} cases graded)")
    print(
        f"  ambiguity recall (should-flag): {ambiguity_recall:.0%}"
        f"  ({len(ambiguous_expected)} cases graded)"
    )
    print(
        f"  service-hint accuracy:        {service_acc:.0%}  ({len(service_checked)} cases graded)"
    )
    print(f"  false-confidence rate:        {len(false_confident)}/{total} wrong-but-confident")

    total_input_tokens = sum(r.input_tokens for r in results)
    total_output_tokens = sum(r.output_tokens for r in results)
    est_cost = (
        total_input_tokens * EST_USD_PER_INPUT_TOKEN
        + total_output_tokens * EST_USD_PER_OUTPUT_TOKEN
    )
    print(
        f"  tokens:                       {total_input_tokens} in / {total_output_tokens} out"
        f"  (~${est_cost:.4f} est., illustrative only)\n"
    )

    failures = [r for r in results if not r.ok]
    if failures:
        print(f"{len(failures)} failing case(s):\n")
        for r in failures:
            failed_checks = [name for name, passed in r.checks.items() if not passed]
            print(f"  [{r.id}] {r.message!r}")
            print(
                f"      failed={failed_checks} predicted_intent={r.predicted_intent} "
                f"ambiguous={r.predicted_is_ambiguous} confidence={r.confidence:.2f}"
            )
            for note in r.notes:
                print(f"      {note}")
        print()

    report = {
        "total": total,
        "intent_accuracy": intent_acc,
        "ambiguity_recall": ambiguity_recall,
        "service_accuracy": service_acc,
        "false_confidence_count": len(false_confident),
        "failures": [r.id for r in failures],
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "estimated_cost_usd": round(est_cost, 4),
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2))
    print(f"Report written to {REPORT_PATH}")

    passed = intent_acc >= INTENT_ACCURACY_FLOOR and ambiguity_recall >= AMBIGUITY_RECALL_FLOOR
    if not passed:
        print(
            f"\nBelow regression floor (intent >= {INTENT_ACCURACY_FLOOR:.0%}, "
            f"ambiguity recall >= {AMBIGUITY_RECALL_FLOOR:.0%})."
        )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
