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


def test_snapshot_target_does_not_duplicate_accessible_name() -> None:
    snapshot = parse_snapshot(SNAPSHOT)
    element = snapshot.by_ref("e3")
    assert element is not None
    target = element.to_target(description="Current Bill", page_url=snapshot.url)
    assert target.name == "Current Bill"
    assert target.label is None
    assert target.text is None
    assert target.recorded_description is None

    described = element.to_target(description="billing action", page_url=snapshot.url)
    assert described.recorded_description == "billing action"


def test_legacy_label_and_text_targets_still_resolve() -> None:
    snapshot = parse_snapshot(SNAPSHOT)
    assert snapshot.resolve(ElementTarget(role="textbox", label="Account number")) is not None
    assert snapshot.resolve(ElementTarget(role="button", text="Current Bill")) is not None


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

