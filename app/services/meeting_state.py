"""MeetingState dataclass and versioned snapshots.

Versions are append-only and strictly monotonic per meeting.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any

from app.exceptions import MeetingAIError
from app.models import MeetingStateVersion
from app.web.extensions import db


@dataclass
class MeetingState:
    agenda: str = ""
    decisions: list = field(default_factory=list)
    action_items: list = field(default_factory=list)
    open_questions: list = field(default_factory=list)
    current_topic: str = ""
    participants: list = field(default_factory=list)
    last_ai_turn: dict | None = None
    rolling_summary: str = ""
    confirmed_facts: list = field(default_factory=list)
    risks: list = field(default_factory=list)
    # Evidence-backed, per-speaker discussion views.  This is intentionally
    # part of versioned state rather than a separate mutable cache so live
    # clients and the final summary always observe the same attribution.
    speaker_views: list = field(default_factory=list)
    version: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "MeetingState":
        data = data or {}
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


def merge_patch(
    base: dict[str, Any],
    patch: dict[str, Any],
    *,
    replace_fields: set[str] | None = None,
) -> dict[str, Any]:
    """Deep-merge a state patch. Lists of dicts with `id` are upserted; other lists append unique items."""
    result = deepcopy(base)
    replace_fields = replace_fields or set()
    for key, value in (patch or {}).items():
        if key == "version":
            continue
        if key in replace_fields:
            result[key] = deepcopy(value)
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_patch(result[key], value)
        elif isinstance(value, list):
            existing = result.get(key)
            if not isinstance(existing, list):
                result[key] = deepcopy(value)
                continue
            result[key] = _merge_list(existing, value)
        else:
            result[key] = deepcopy(value)
    return result


def _merge_list(existing: list, incoming: list) -> list:
    if not incoming:
        return deepcopy(existing)
    if incoming and all(isinstance(x, dict) and "id" in x for x in incoming):
        by_id = {item.get("id"): deepcopy(item) for item in existing if isinstance(item, dict) and "id" in item}
        order = [item.get("id") for item in existing if isinstance(item, dict) and "id" in item]
        for item in incoming:
            iid = item.get("id")
            if iid in by_id:
                by_id[iid] = merge_patch(by_id[iid], item)
            else:
                by_id[iid] = deepcopy(item)
                order.append(iid)
        extras = [deepcopy(x) for x in existing if not (isinstance(x, dict) and "id" in x)]
        return extras + [by_id[i] for i in order if i in by_id]
    out = list(existing)
    for item in incoming:
        if item not in out:
            out.append(deepcopy(item))
    return out


class MeetingStateService:
    def current(self, meeting_id: str) -> MeetingState:
        row = (
            MeetingStateVersion.query.filter_by(meeting_id=meeting_id)
            .order_by(MeetingStateVersion.version.desc())
            .first()
        )
        if not row:
            return MeetingState()
        return MeetingState.from_dict(row.snapshot)

    def latest_version(self, meeting_id: str) -> int:
        row = (
            MeetingStateVersion.query.filter_by(meeting_id=meeting_id)
            .order_by(MeetingStateVersion.version.desc())
            .first()
        )
        return int(row.version) if row else 0

    def apply_patch(
        self,
        meeting_id: str,
        patch: dict[str, Any],
        *,
        replace_fields: set[str] | None = None,
    ) -> MeetingState:
        current = self.current(meeting_id)
        merged = merge_patch(current.to_dict(), patch or {}, replace_fields=replace_fields)
        next_version = self.latest_version(meeting_id) + 1
        if next_version <= self.latest_version(meeting_id):
            raise MeetingAIError("Meeting state version must be monotonic")
        merged["version"] = next_version
        snap = MeetingState.from_dict(merged)
        row = MeetingStateVersion(
            meeting_id=meeting_id,
            version=next_version,
            snapshot=snap.to_dict(),
        )
        db.session.add(row)
        db.session.commit()
        return snap

    def replace(self, meeting_id: str, state: MeetingState) -> MeetingState:
        return self.apply_patch(meeting_id, state.to_dict())
