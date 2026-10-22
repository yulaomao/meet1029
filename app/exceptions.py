"""Domain exceptions."""


class MeetingAIError(Exception):
    """Base error."""


class IllegalStateTransition(MeetingAIError):
    def __init__(self, current: str, target: str):
        super().__init__(f"Illegal meeting status transition: {current} -> {target}")
        self.current = current
        self.target = target


class RawTextImmutableError(MeetingAIError):
    def __init__(self, segment_id: str):
        super().__init__(f"raw_text is immutable after insert (segment={segment_id})")
        self.segment_id = segment_id


class PermissionDenied(MeetingAIError):
    def __init__(self, action: str, role: str):
        super().__init__(f"Role {role} cannot {action}")
        self.action = action
        self.role = role


class HostConflictError(MeetingAIError):
    def __init__(self, meeting_id: str, host_client_id: str):
        super().__init__(f"Meeting {meeting_id} already has recording host {host_client_id}")
        self.meeting_id = meeting_id
        self.host_client_id = host_client_id


class HostTransferGapError(MeetingAIError):
    def __init__(self, gap_ms: int, max_ms: int):
        super().__init__(f"Host transfer gap {gap_ms}ms exceeds {max_ms}ms")
        self.gap_ms = gap_ms
        self.max_ms = max_ms


class ConsentRequired(MeetingAIError):
    def __init__(self):
        super().__init__("Consent checkbox is required before starting a meeting")


class ChunkError(MeetingAIError):
    pass


class ProviderUnavailable(MeetingAIError):
    pass


class PlatformNotSupported(MeetingAIError):
    def __init__(self, provider: str, platform: str):
        super().__init__(
            f"{provider} requires Apple Silicon / MLX and cannot load on {platform}. "
            "Use Fake* or CPU providers on Linux."
        )
        self.provider = provider
        self.platform = platform
