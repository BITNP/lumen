"""The output capability this module advertises: JSON Schema + semantics.

The capability description is the module's contract with the core Agent: it is
injected into the system prompt through the waycast capability catalog, so it
carries the per-sentence unit guidance (ADR-0003) without touching core code.
The hub validates every dispatched unit against OUTPUT_SCHEMA before delivery.
"""
from __future__ import annotations

from typing import Any, Final

MODULE_NAME: Final = "presenter"

OUTPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "speech": {
            "type": "string",
            "minLength": 1,
            "description": "one sentence to speak aloud (TTS + playback + subtitle source)",
        },
        "subtitle": {
            "type": "string",
            "minLength": 1,
            "description": "subtitle text override; defaults to speech",
        },
        "emotion": {
            "type": "string",
            "minLength": 1,
            "description": "emotion tag for the avatar (e.g. happy, shy, serious)",
        },
        "motion": {
            "type": "string",
            "minLength": 1,
            "description": "motion tag for the avatar (e.g. wave, nod)",
        },
        "control": {
            "type": "string",
            "enum": ["stop"],
            "description": (
                "stop control unit: clears the expression queue and halts current "
                "playback immediately; send it as its own unit before new speech"
            ),
        },
    },
    "additionalProperties": False,
    "minProperties": 1,
}

CAPABILITY_OUTPUT_DESCRIPTION: Final = (
    "Raspberry Girl's aggregate output module (voice + avatar + subtitles + PPT). "
    "Emit ONE unit PER SPOKEN SENTENCE; the list order is the exact performance "
    "order, and emotion/motion tags ride on the sentence they accompany. Each unit "
    "optionally carries speech (one sentence to speak), subtitle (defaults to "
    "speech), emotion and motion tags. To interrupt her current speech, first send "
    'a unit {"control": "stop"} then the new sentences. Accepting a unit only means '
    "queued: when the whole dispatched list has finished playing, a queue_finished "
    "input frame arrives carrying a summary; use status_presenter to check "
    "playback progress, current PPT page or the active TTS engine at any time."
)

CAPABILITY_INPUT_DESCRIPTION: Final = (
    "events from Raspberry Girl's frontend screen: user_interaction (a click or "
    "request from the viewer, with the touched element in the metadata) and "
    "queue_finished (the last dispatched expression finished playing; the input "
    "content says how many sentences completed and which PPT page is showing; an "
    "interrupted variant reports the queue was cleared). queue_finished is the "
    "heartbeat that lets her decide the next step of her presentation."
)

CAPABILITY_STATUS_DESCRIPTION: Final = (
    "live presentation state: tts_engine (active engine), playing, queue_depth, "
    "completed_sentences (current round), presentation_available (Godot window "
    "connected), page/deck (PPT state from M3 on)"
)

__all__ = [
    "MODULE_NAME",
    "OUTPUT_SCHEMA",
    "CAPABILITY_OUTPUT_DESCRIPTION",
    "CAPABILITY_INPUT_DESCRIPTION",
    "CAPABILITY_STATUS_DESCRIPTION",
]
