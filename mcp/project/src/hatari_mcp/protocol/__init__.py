"""HMCP v1 protocol primitives and legacy capability negotiation."""

from .capabilities import CANONICAL_CAPABILITIES, describe, legacy_report
from .frame import (
    ABSOLUTE_MAX_PAYLOAD,
    DEFAULT_MAX_PAYLOAD,
    HEADER_SIZE,
    MAGIC,
    PROTOCOL_VERSION,
    Flags,
    Frame,
    FrameDecoder,
    FrameEncoder,
    FrameType,
)
from .hello import Banner, HelloAck, find_banner, parse_banner_line

__all__ = [
    "ABSOLUTE_MAX_PAYLOAD",
    "Banner",
    "CANONICAL_CAPABILITIES",
    "DEFAULT_MAX_PAYLOAD",
    "Flags",
    "Frame",
    "FrameDecoder",
    "FrameEncoder",
    "FrameType",
    "HEADER_SIZE",
    "HelloAck",
    "MAGIC",
    "PROTOCOL_VERSION",
    "describe",
    "find_banner",
    "legacy_report",
    "parse_banner_line",
]
