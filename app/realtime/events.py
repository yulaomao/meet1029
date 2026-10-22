"""Canonical Socket.IO event names for the /live namespace."""

NAMESPACE = "/live"

CLIENT_EVENTS = (
    "join_meeting",
    "leave_meeting",
    "audio_frame",
    "heartbeat",
    "meeting_state_patch",
)

SERVER_EVENTS = (
    "transcript_partial",
    "transcript_final",
    "meeting_state_patch",
    "ai_status",
    "ai_token",
    "tts_audio",
    "host_changed",
    "client_health",
    "recording_status",
    "error",
)
