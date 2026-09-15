from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .workflow import ElementTarget

_URL_RE = re.compile(r"^\s*-\s*Page URL:\s*(.*)$")
_TITLE_RE = re.compile(r"^\s*-\s*Page Title:\s*(.*)$")
_ELEMENT_RE = re.compile(
    r"^\s*-\s*(?P<role>[A-Za-z][A-Za-z0-9_-]*)"
    r'(?:\s+"(?P<name>.*?)")?'
    r"(?P<suffix>.*?)\[ref=(?P<ref>[^\]]+)\]"
)
_ATTRIBUTE_RE = re.compile(r"\[([A-Za-z][A-Za-z0-9_-]*)=([^\]]+)\]")
_STABLE_ATTRIBUTE_NAMES = {"aria-label", "data-testid", "name", "placeholder", "title"}


def _norm(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


@dataclass(slots=True)
class SnapshotElement:
    ref: str
    role: str
    name: str | None
    attributes: dict[str, str] = field(default_factory=dict)
    nearby_text: list[str] = field(default_factory=list)

    def to_target(
        self,
        *,
        description: str | None,
        page_url: str | None,
        locator: str | None = None,
    ) -> ElementTarget:
        stable = {k: v for k, v in self.attributes.items() if k in _STABLE_ATTRIBUTE_NAMES}
        label = self.name if self.role in {"textbox", "combobox", "checkbox", "radio"} else None
        text = self.name if self.role in {"button", "link", "option", "menuitem"} else None
        return ElementTarget(
            role=self.role,
            name=self.name,
            locator=locator,
            label=label,
            text=text,
            stable_attributes=stable,
            nearby_text=self.nearby_text,
            page_url_prefix=page_url,
            recorded_description=description,
        )


@dataclass(slots=True)
class PageSnapshot:
    raw: str
    url: str | None
    title: str | None
    elements: list[SnapshotElement]

    def by_ref(self, ref: str) -> SnapshotElement | None:
        return next((element for element in self.elements if element.ref == ref), None)

    def resolve_target(self, *, role: str, name: str) -> SnapshotElement | None:
        matches = [
            element
            for element in self.elements
            if _norm(element.role) == _norm(role) and _norm(element.name) == _norm(name)
        ]
        return matches[0] if len(matches) == 1 else None

    def resolve(self, target: ElementTarget) -> SnapshotElement | None:
        exact = [element for element in self.elements if _matches_exact(element, target)]
        return exact[0] if len(exact) == 1 else None

    def ranked_candidates(
        self, target: ElementTarget, limit: int = 8
    ) -> list[tuple[SnapshotElement, float]]:
        candidates: list[tuple[SnapshotElement, float]] = []
        for element in self.elements:
            score = _candidate_score(element, target)
            if score > 0:
                candidates.append((element, score))
        candidates.sort(key=lambda item: (-item[1], item[0].ref))
        return candidates[:limit]


def parse_snapshot(text: str) -> PageSnapshot:
    lines = text.splitlines()
    url: str | None = None
    title: str | None = None
    elements: list[SnapshotElement] = []

    for index, line in enumerate(lines):
        if match := _URL_RE.match(line):
            url = match.group(1).strip()
        elif match := _TITLE_RE.match(line):
            title = match.group(1).strip()

        match = _ELEMENT_RE.match(line)
        if not match:
            continue

        attrs = {
            key: value.strip('"') for key, value in _ATTRIBUTE_RE.findall(match.group("suffix"))
        }
        nearby: list[str] = []
        for surrounding in lines[max(0, index - 2) : min(len(lines), index + 3)]:
            stripped = surrounding.strip()
            if stripped and stripped != line.strip() and "[ref=" not in stripped:
                nearby.append(stripped[:240])
        elements.append(
            SnapshotElement(
                ref=match.group("ref"),
                role=match.group("role"),
                name=match.group("name"),
                attributes=attrs,
                nearby_text=nearby[:4],
            )
        )

    return PageSnapshot(raw=text, url=url, title=title, elements=elements)


def _matches_exact(element: SnapshotElement, target: ElementTarget) -> bool:
    if target.role and _norm(element.role) != _norm(target.role):
        return False
    identity_signals = [target.name, target.label, target.text]
    for signal in identity_signals:
        if signal and _norm(element.name) != _norm(signal):
            return False
    for key, expected in target.stable_attributes.items():
        actual = element.attributes.get(key)
        if actual is None or _norm(actual) != _norm(expected):
            return False
    if not any(identity_signals) and target.recorded_description:
        return _norm(element.name) == _norm(target.recorded_description)
    return True


def _candidate_score(element: SnapshotElement, target: ElementTarget) -> float:
    score = 0.0
    if target.role:
        if _norm(element.role) == _norm(target.role):
            score += 0.45
        else:
            score -= 0.3
    desired_name = target.name or target.label or target.text or target.recorded_description
    if desired_name and element.name:
        if _norm(desired_name) == _norm(element.name):
            score += 0.5
        else:
            score += 0.35 * SequenceMatcher(None, _norm(desired_name), _norm(element.name)).ratio()
    if target.stable_attributes:
        matched = sum(
            1
            for key, value in target.stable_attributes.items()
            if _norm(element.attributes.get(key)) == _norm(value)
        )
        score += 0.2 * matched / len(target.stable_attributes)
    return max(0.0, min(score, 1.0))
