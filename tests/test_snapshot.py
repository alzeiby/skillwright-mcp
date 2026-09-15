from __future__ import annotations

from skillwright_mcp.snapshot import parse_snapshot
from skillwright_mcp.workflow import ElementTarget

SNAPSHOT = """\
### Page
- Page URL: https://example.test/billing
- Page Title: Billing
### Snapshot
```yaml
- main [ref=e1]:
  - textbox "Account number" [ref=e2]
  - button "Current Bill" [ref=e3]
  - button "Payment History" [ref=e4]
```
"""


def test_parse_snapshot_and_resolve_unique_semantic_target() -> None:
    snapshot = parse_snapshot(SNAPSHOT)
    assert snapshot.url == "https://example.test/billing"
    assert snapshot.title == "Billing"
    resolved = snapshot.resolve(ElementTarget(role="button", name="Current Bill"))
    assert resolved is not None
    assert resolved.ref == "e3"


def test_ambiguous_target_is_not_resolved() -> None:
    snapshot = parse_snapshot(
        SNAPSHOT.replace('button "Payment History"', 'button "Current Bill"')
    )
    assert snapshot.resolve(ElementTarget(role="button", name="Current Bill")) is None


def test_candidate_ranking_is_advisory_for_repairs() -> None:
    snapshot = parse_snapshot(SNAPSHOT.replace("Current Bill", "View Latest Statement"))
    candidates = snapshot.ranked_candidates(ElementTarget(role="button", name="Current Bill"))
    assert candidates
    assert all(element.ref for element, _ in candidates)

