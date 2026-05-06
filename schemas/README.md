# Schemas

Formal JSON Schema (Draft 2020-12) definitions for the cross-component contracts referenced by Artefact A of the Master Prompt Package (`docs/master-prompt-package.md`). The pseudo-JSON blocks in the spec are illustrative for human readers — these files are the machine-validated contracts.

## Files

| File | Used by | Flow |
|---|---|---|
| `proposed_order.schema.json` | Alpha agents → Risk Engine → Execution | Every order on every leg. |
| `strategy_report.schema.json` | Alpha agents → Memory, Learning, HITL | Emitted at every lifecycle phase boundary. |
| `hitl_packet.schema.json` | Any agent → HITL Interface → Principal | Approval requests with structured options. |
| `compliance_verdict.schema.json` | Compliance Agent → PR Agent / HITL | Per-draft gating verdict. |

## Validation

Validate at every component boundary. Implementation suggestion (Python):

```python
import json, jsonschema
schema = json.load(open("schemas/proposed_order.schema.json"))
jsonschema.Draft202012Validator(schema).validate(order_dict)
```

A failed validation MUST translate to `{"action": "no_op", "reason": "schema_failure"}` per the master prompt's hardening rules.

## Conventions

- **Money fields** use `_usd` or `_bps` suffixes; agents emit canonical `size_usd` and engines compute `size_contracts` at submission time.
- **Identifiers** that must be deterministic (`client_order_id`) are SHA-256 hex.
- **Probabilities** and **fractions of NAV / ADV** are bounded `[0, 1]`.
- **Drawdowns** (`max_DD`) are non-positive numbers.
- **Per-DEX-venue orders** require a non-`none` `mev_route` and `max_slippage_bps <= 50` — enforced in `proposed_order.schema.json` via conditional `if`/`then`.

## Out of scope (for now)

The spec also describes typed payloads for `MarketDataChunk`, `OnchainFeature`, `Sentiment`, `MacroFeature`, `RiskEngineVerdict`, `OrchestratorTick`, `PortfolioWeights`, `ExecutionFills`, and `PRDraft`. They will be added once the Principal ratifies framework choice (LangGraph vs CrewAI vs custom), since the field naming conventions for those depend on whether they are emitted from Python dataclasses, TypeScript interfaces, or pydantic models.
