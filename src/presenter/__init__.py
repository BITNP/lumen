"""Raspberry Girl aggregate output module (waycast ADR-0010).

The host process is the waycast module identity: it connects to the hub,
executes output units (TTS -> playback -> subtitles -> presentation), returns
output results, and sends frontend interactions back as input frames.
"""
