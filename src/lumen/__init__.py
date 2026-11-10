"""Raspberry Girl aggregate output module (waycast ADR-0010).

The host process is the waycast module identity: it connects to the hub,
executes output units through the unified playback queue (TTS + playback +
subtitles + presentation, ADR-0008), returns output results, and sends
queue_finished back to the hub as its only input frame (ADR-0005).
"""
