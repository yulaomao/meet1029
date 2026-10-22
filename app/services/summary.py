"""Post-meeting LLM summary using structured JSON from the LLM provider."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from hashlib import sha1
from typing import Any

from app.models import ActionItem, Decision, Meeting, MeetingSummary, TranscriptSegment
from app.providers.base import LLMProvider
from app.providers.llm import FAKE_SUMMARY
from app.realtime.echo import AI_SPEAKER
from app.services.meeting_state import MeetingStateService
from app.services.transcript import preferred_segments
from app.web.extensions import db


VALID_STANCES = frozenset({"支持", "反对", "保留", "建议", "未明确"})
_GENERIC_SPEAKER = re.compile(r"^(?:speaker|说话人)\s*([0-9]+)?$", re.I)


def _display_time(milliseconds: int) -> str:
    seconds = max(0, int(milliseconds or 0) // 1000)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def is_human_final_segment(segment: TranscriptSegment) -> bool:
    """Whether a segment is safe to use as an attendee statement."""
    text = (segment.display_text or segment.corrected_text or segment.punctuated_text or segment.raw_text or "").strip()
    return bool(text) and segment.speaker_label != AI_SPEAKER and (segment.asr_state or "final") == "final"


def speaker_identity(segment: TranscriptSegment) -> dict[str, Any]:
    """Return a conservative, stable identity without turning guesses into names."""
    confidence = max(0.0, min(1.0, float(segment.speaker_confidence or 0.0)))
    raw_label = (segment.speaker_label or "").strip() or "未知说话人"
    generic = _GENERIC_SPEAKER.match(raw_label)
    neutral_label = f"说话人 {generic.group(1)}" if generic and generic.group(1) else "未知说话人"
    if segment.is_overlap:
        return {
            "speaker_key": f"overlap:{segment.id}",
            "speaker_label": "重叠发言",
            "identity_confidence": 0.0,
            "identity_status": "overlap",
        }
    if segment.speaker_user_id and confidence >= 0.75:
        return {
            "speaker_key": f"user:{segment.speaker_user_id}",
            "speaker_label": raw_label,
            "identity_confidence": confidence,
            "identity_status": "verified",
        }
    if segment.speaker_cluster_id and confidence >= 0.45:
        return {
            "speaker_key": f"cluster:{segment.speaker_cluster_id}",
            "speaker_label": neutral_label if generic else raw_label,
            "identity_confidence": confidence,
            "identity_status": "clustered",
        }
    if generic:
        label = neutral_label
    elif raw_label in {"未知", "未知说话人", "成员"}:
        label = "未知说话人"
    else:
        # Keep an observed/manual label visible but make its verification state
        # explicit; Qwen must not promote it to a confirmed participant name.
        label = f"{raw_label}（身份待确认）"
    return {
        "speaker_key": f"label:{raw_label}",
        "speaker_label": label,
        "identity_confidence": confidence,
        "identity_status": "unverified",
    }


def attributed_segments(segments: list[TranscriptSegment]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for segment in segments:
        if not is_human_final_segment(segment):
            continue
        identity = speaker_identity(segment)
        rows.append(
            {
                "id": segment.id,
                "start_ms": int(segment.start_ms or 0),
                "time": _display_time(segment.start_ms),
                "text": (segment.display_text or segment.corrected_text or segment.punctuated_text or segment.raw_text or "").strip(),
                **identity,
            }
        )
    return rows


def attributed_transcript(segments: list[TranscriptSegment]) -> str:
    """LLM input carrying source ID and conservative speaker provenance."""
    return "\n".join(
        "[segment:{id} | {time} | speaker_key:{speaker_key} | {speaker_label} | "
        "identity:{identity_status}/{identity_confidence:.2f}] {text}".format(**row)
        for row in attributed_segments(segments)
    )


def summary_transcript(segments: list[TranscriptSegment]) -> str:
    """Meeting context for the final summary, including assistant responses.

    Assistant replies are not evidence of an attendee's viewpoint, therefore
    ``attributed_transcript`` correctly excludes them from speaker views. They
    are still substantive parts of an AI-assisted meeting: a final summary
    must record what the assistant explained or proposed, clearly as AI output
    rather than as a participant decision.
    """
    rows: list[str] = []
    for segment in segments:
        text = (segment.display_text or segment.corrected_text or segment.punctuated_text or segment.raw_text or "").strip()
        if not text or (segment.asr_state or "final") != "final":
            continue
        prefix = "assistant" if segment.speaker_label == AI_SPEAKER else "participant"
        identity = "AI" if prefix == "assistant" else speaker_identity(segment)["identity_status"]
        rows.append(
            f"[segment:{segment.id} | {_display_time(segment.start_ms)} | role:{prefix} | "
            f"speaker:{segment.speaker_label or '未知说话人'} | identity:{identity}] {text}"
        )
    return "\n".join(rows)


def fallback_summary(segments: list[TranscriptSegment]) -> str:
    """Produce a factual, useful summary when the local model omits it.

    A valid JSON envelope containing only speaker views is still an incomplete
    post-meeting result.  Keep the fallback deliberately extractive: it makes
    no decisions or claims beyond the final transcript and preserves speaker
    provenance for a user inspecting an imported recording.
    """
    excerpts: list[str] = []
    for segment in segments:
        text = re.sub(r"\s+", " ", segment.display_text or segment.corrected_text or segment.punctuated_text or segment.raw_text or "").strip()
        if not text:
            continue
        speaker = "成就精灵（AI）" if segment.speaker_label == AI_SPEAKER else (segment.speaker_label or "未知说话人")
        excerpt = text[:120].rstrip("，、；：。！？!?")
        item = f"{_display_time(segment.start_ms)} {speaker}：{excerpt}"
        if item not in excerpts:
            excerpts.append(item)
        if len(excerpts) >= 8:
            break
    if not excerpts:
        return "未检测到可用于总结的最终会议发言。"
    return "会议逐字稿已生成。以下为按时间整理的主要讨论与 AI 回应：" + "；".join(excerpts) + "。"


def _source_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, (str, int))]
    return []


def _evidence_point(value: Any, allowed_ids: set[str], *, stance: bool = False) -> dict[str, Any] | None:
    if isinstance(value, str):
        return None  # A claim without an explicit source is never promoted.
    if not isinstance(value, dict):
        return None
    text = str(value.get("text") or value.get("claim") or value.get("content") or "").strip()
    source_ids = _source_ids(value.get("source_segment_ids") or value.get("source_segment_id"))
    source_ids = list(dict.fromkeys(source_ids))
    if not text or len(text) > 360 or not source_ids or any(source not in allowed_ids for source in source_ids):
        return None
    result: dict[str, Any] = {"text": text, "source_segment_ids": source_ids}
    if stance:
        position = str(value.get("position") or value.get("stance") or "").strip()
        if position not in VALID_STANCES:
            return None
        result["position"] = position
    return result


def validate_speaker_views(payload: dict[str, Any], segments: list[TranscriptSegment]) -> list[dict[str, Any]]:
    """Validate all claims against the source speaker and source segment IDs.

    A model may summarize, but it must never decide an attribution from prose
    alone.  Every retained item therefore has one or more IDs belonging to the
    same stable speaker identity as the enclosing view.
    """
    raw_views = payload.get("speaker_views") if isinstance(payload, dict) else None
    if not isinstance(raw_views, list):
        return []
    rows = attributed_segments(segments)
    valid: list[dict[str, Any]] = []
    for item in raw_views:
        if not isinstance(item, dict):
            continue
        key = str(item.get("speaker_key") or "").strip()
        topic = str(item.get("topic") or "").strip()
        if not key or not topic or len(topic) > 120:
            continue
        speaker_rows = [row for row in rows if row["speaker_key"] == key]
        if not speaker_rows:
            continue
        speaker_ids = {row["id"] for row in speaker_rows}
        normalized: dict[str, Any] = {
            "id": f"{key}:{sha1(topic.encode('utf-8')).hexdigest()[:12]}",
            "speaker_key": key,
            "speaker_label": speaker_rows[0]["speaker_label"],
            "identity_confidence": max(row["identity_confidence"] for row in speaker_rows),
            "identity_status": speaker_rows[0]["identity_status"],
            "topic": topic,
            "explicit_stances": [],
            "key_points": [],
            "suggestions": [],
            "concerns": [],
            "open_questions": [],
            "possible_tendencies": [],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        for source_name, output_name, requires_stance in (
            ("explicit_stances", "explicit_stances", True),
            ("key_points", "key_points", False),
            ("suggestions", "suggestions", False),
            ("concerns", "concerns", False),
            ("open_questions", "open_questions", False),
            ("possible_tendencies", "possible_tendencies", False),
        ):
            values = item.get(source_name) or []
            if not isinstance(values, list):
                continue
            for candidate in values:
                point = _evidence_point(candidate, speaker_ids, stance=requires_stance)
                if point and point not in normalized[output_name]:
                    if output_name == "possible_tendencies":
                        point["confidence"] = "low"
                    normalized[output_name].append(point)
        if any(normalized[name] for name in ("explicit_stances", "key_points", "suggestions", "concerns", "open_questions", "possible_tendencies")):
            valid.append(normalized)
    return valid


def parse_json_payload(text: str, *, fallback: dict | None = None) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw)
        raw = re.sub(r"```$", "", raw).strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return dict(FAKE_SUMMARY if fallback is None else fallback)
    return dict(FAKE_SUMMARY if fallback is None else fallback)


class SummaryService:
    def __init__(self, llm: LLMProvider):
        self.llm = llm

    def analyze(self, meeting: Meeting) -> MeetingSummary:
        # Keep AI answers in the meeting narrative, but retain the stricter
        # human-only attribution path below for participant viewpoints.
        segs = preferred_segments(meeting.id, include_assistant=True)
        transcript = summary_transcript(segs)
        prompt = (
            "请根据会议逐字稿输出严格 JSON，字段: title, summary, topics, decisions, action_items, "
            "risks, open_questions, follow_ups, speaker_views。decision 和 action_item 必须含 source_segment_id，"
            "且只能填写下面出现的 segment ID。title 是 8 至 28 个字的中文会议标题，不要有引号或标点；"
            "summary 是面向参会人的实质性会议纪要：当记录至少有 5 个有效片段时，使用 180 至 500 个汉字，"
            "按“讨论背景/问题与澄清/AI 已给出的解释或建议/已经确认的结论/待确认事项”组织；"
            "没有明确结论时必须明确写“尚未形成决定”，不能为了简短只罗列前三句原文。"
            "role:assistant 是成就精灵的实际回复：应概括其建议或解释，但必须标注为 AI 的回答或建议，"
            "不得把它改写为参会人已经同意的决定。不要编造原文没有的事实。speaker_views 必须是数组；每项含 speaker_key、topic、"
            "explicit_stances、key_points、suggestions、concerns、open_questions、possible_tendencies。每个观点项都必须"
            "含 text 和 source_segment_ids（数组），且其 ID 必须属于同一 speaker_key。explicit_stances"
            "的每项还必须含 position，只能为 支持、反对、保留、建议、未明确。只有原文明说的立场才能"
            "放入 explicit_stances；基于上下文的谨慎判断只能放入 possible_tendencies，不能当成事实。"
            "speaker_views 只能来自 role:participant，且不得把 identity:unverified、overlap 或未知说话人改写为实名。"
            "所有数组字段即使为空也必须输出 []，summary 不得为空。\n\n"
            + transcript
        )
        # A malformed local-model response must never turn into FAKE_SUMMARY:
        # that fixture describes an unrelated historical meeting and would
        # contaminate a real attendee-facing record.
        payload = parse_json_payload(self.llm.complete(prompt, json_mode=True), fallback={})
        if not str(payload.get("summary") or "").strip():
            payload["summary"] = fallback_summary(segs)
        valid_views = validate_speaker_views(payload, segs)
        raw_views = payload.get("speaker_views")
        # A malformed non-empty model response is not evidence that the
        # meeting has no viewpoints.  Keep the prior validated state instead.
        if isinstance(raw_views, list) and raw_views and not valid_views:
            valid_views = MeetingStateService().current(meeting.id).speaker_views
        payload["speaker_views"] = valid_views
        # Qwen may use semantically equivalent field names despite the schema
        # instruction (``content`` for a decision, ``task`` for an action).
        # Normalize them before persistence so the attendee-facing panels never
        # contain blank action rows merely because the model chose a synonym.
        normalized_decisions: list[dict[str, Any]] = []
        for decision in payload.get("decisions") or []:
            if isinstance(decision, str):
                decision = {"text": decision}
            if not isinstance(decision, dict):
                continue
            text = str(decision.get("text") or decision.get("content") or decision.get("decision") or "").strip()
            if text:
                normalized_decisions.append(
                    {"text": text, "source_segment_id": decision.get("source_segment_id")}
                )
        normalized_actions: list[dict[str, Any]] = []
        for action in payload.get("action_items") or []:
            if isinstance(action, str):
                action = {"description": action}
            if not isinstance(action, dict):
                continue
            description = str(
                action.get("description") or action.get("task") or action.get("text") or action.get("content") or ""
            ).strip()
            if description:
                normalized_actions.append(
                    {
                        "description": description,
                        "owner": str(action.get("owner") or "").strip(),
                        "deadline": str(action.get("deadline") or "").strip(),
                        "priority": str(action.get("priority") or "normal").strip() or "normal",
                        "source_segment_id": action.get("source_segment_id"),
                    }
                )
        payload["decisions"] = normalized_decisions
        payload["action_items"] = normalized_actions
        summary = MeetingSummary(
            meeting_id=meeting.id,
            summary=payload.get("summary") or "",
            topics=payload.get("topics") or [],
            risks=payload.get("risks") or [],
            open_questions=payload.get("open_questions") or [],
            follow_ups=payload.get("follow_ups") or [],
            payload=payload,
        )
        db.session.add(summary)
        for d in normalized_decisions:
            db.session.add(
                Decision(
                    meeting_id=meeting.id,
                    text=d["text"],
                    source_segment_id=self._source_id(d.get("source_segment_id"), d["text"], segs),
                )
            )
        for a in normalized_actions:
            db.session.add(
                ActionItem(
                    meeting_id=meeting.id,
                    description=a["description"],
                    owner=a["owner"],
                    deadline=a["deadline"],
                    priority=a["priority"],
                    source_segment_id=self._source_id(a.get("source_segment_id"), a["description"], segs),
                    status="未开始",
                )
            )
        db.session.commit()
        # A final analysis supersedes any incremental view.  Replacement is
        # deliberate: list merge would leave stale claims after a correction.
        MeetingStateService().apply_patch(
            meeting.id,
            {
                "rolling_summary": payload.get("summary") or "",
                "speaker_views": payload["speaker_views"],
            },
            replace_fields={"speaker_views"},
        )
        return summary

    @staticmethod
    def _source_id(candidate: str | None, text: str, segments: list[TranscriptSegment]) -> str | None:
        ids = {segment.id for segment in segments}
        if candidate in ids:
            return candidate
        # Fake/older local models sometimes omit citations.  Preserve a traceable
        # fallback only when at least two meaningful Chinese characters overlap.
        chars = {ch for ch in (text or "") if "\u4e00" <= ch <= "\u9fff"}
        if len(chars) < 2:
            return None
        best, score = None, 0
        for segment in segments:
            segment_chars = set(segment.display_text or segment.corrected_text or segment.raw_text or "")
            overlap = len(chars & segment_chars)
            if overlap > score:
                best, score = segment, overlap
        return best.id if best is not None and score >= 2 else None
