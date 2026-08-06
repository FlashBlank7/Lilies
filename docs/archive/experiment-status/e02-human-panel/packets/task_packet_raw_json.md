# E02 task packet: raw JSON evidence

Packet type: `raw_json`

Status: prepared_pending_external_execution

## Task Prompt

Identify the failed requirement or workflow surface, choose the most likely repair target, and write the first repair action you would recommend to an engineer. Submit only when the answer is actionable enough for another engineer to begin repair.

## Output Fields

- Failed requirement or surface:
- Most likely repair target:
- First repair action:
- Confidence from 1 to 5:

## Source

- Evidence file: `../../evidence/experiment_v0.2.36_e02_readable_testframe_review_2026_07_09.json`
- Packet path inside evidence: `packets.raw_legacy_json`
- Condition id for capture sheet: `raw_json`
- Task id for capture sheet: `e02_raw_json_v0.2.36`

## Evidence Excerpt

```json
{
  "passed": false,
  "summary": {
    "total": 2,
    "passed": 0,
    "failed": 2,
    "mandatory_failed": 2
  },
  "tests": [
    {
      "name": "Novel setting adherence",
      "mandatory": true,
      "passed": false,
      "assertions": [
        {
          "passed": false,
          "actual": "Act I: a detective enters the city. Act II: conflict. Act III: resolution.",
          "path": ["outline"],
          "operator": "contains",
          "expected": "magic system",
          "structural": false
        }
      ],
      "tool_evidence": {
        "required_node_types": ["start", "end"],
        "node_types": ["start", "end"],
        "required_node_types_passed": true
      }
    },
    {
      "name": "Visible context assembly",
      "mandatory": true,
      "passed": false,
      "assertions": [
        {
          "passed": false,
          "error": "'assembled_context'",
          "path": ["assembled_context"],
          "operator": "exists",
          "expected": null,
          "structural": false
        }
      ],
      "tool_evidence": {
        "required_node_types": ["start", "end"],
        "node_types": ["start", "end"],
        "required_node_types_passed": true
      }
    }
  ]
}
```
