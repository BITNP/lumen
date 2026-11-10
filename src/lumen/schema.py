"""The output capability this module advertises: JSON Schema + semantics.

The capability description is the module's contract with the core Agent: it is
injected into the system prompt through the waycast capability catalog, so it
carries the per-sentence unit guidance (ADR-0003) without touching core code.
The hub validates every dispatched unit against OUTPUT_SCHEMA before delivery.
"""

from __future__ import annotations

from typing import Any, Final

MODULE_NAME: Final = "lumen"

# Whitelists mirror the frontend (godot/scripts/live2d_avatar.gd): emotion tags
# are face-parameter expression clips (godot/assets/face_expressions/), motion
# tags are the model's motion groups (godot/assets/DAver3.0/motion/).
# tests/test_schema.py pins schema and .gd constants against each other so the
# advertised contract cannot drift from what the avatar can actually perform.
EMOTION_TAGS: Final = ("nod", "shake_head", "wink")
MOTION_TAGS: Final = ("wave", "point", "sweat", "no")

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
            "enum": list(EMOTION_TAGS),
            "description": (
                "facial expression riding on the sentence (expression layer, plays "
                "alongside any motion; fades back to the neutral face when done)"
            ),
        },
        "motion": {
            "type": "string",
            "enum": list(MOTION_TAGS),
            "description": "one-shot body gesture from the avatar's motion groups",
        },
        "control": {
            "type": "string",
            "enum": ["stop"],
            "description": (
                "stop control unit: drops every sentence still queued or being "
                "synthesized; the sentence she is speaking is NOT cut — it finishes, "
                "or the new speech takes over seamlessly the moment its audio is "
                "ready. MANDATORY FIRST UNIT of your list whenever a new input "
                "supersedes what she is currently saying; without it she always "
                "finishes the old answer before starting the new one"
            ),
        },
        "ppt": {
            "type": "object",
            "description": "slide deck instruction executed before this unit's speech",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["open_deck", "goto", "next", "prev", "close"],
                    "description": "open a prepared deck by name, jump, step, or hide",
                },
                "deck": {
                    "type": "string",
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
                    "description": "deck name (exported via tools/prepare_deck.py); required for open_deck",
                },
                "page": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "description": "1-based target page; required for goto",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
            "allOf": [
                {
                    "if": {
                        "properties": {"action": {"const": "goto"}},
                        "required": ["action"],
                    },
                    "then": {"required": ["page"]},
                },
                {
                    "if": {
                        "properties": {"action": {"const": "open_deck"}},
                        "required": ["action"],
                    },
                    "then": {"required": ["deck"]},
                },
            ],
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
    "speech), emotion and motion tags, and a ppt instruction "
    "(ppt.action open_deck+deck to show a prepared slide deck, goto+page, next, "
    "prev, or close) applied right as that sentence starts being spoken. "
    "INTERRUPTION: when a new input supersedes her in-progress answer, your unit "
    'list MUST start with {"control": "stop"} before any new speech — it drops the '
    "old backlog and the new sentences take over her voice the instant they are "
    "synthesized (the sentence mid-play is never cut to silence); without the stop "
    "unit she will always finish the old answer first. Accepting a unit only means "
    "queued: when the whole dispatched list has finished playing, a queue_finished "
    "input frame arrives carrying a summary including the current slide page's "
    "content description and suggested speaker script; use status_lumen to check "
    "playback progress, current PPT deck/page or the active TTS engine at any time."
)

CAPABILITY_INPUT_DESCRIPTION: Final = (
    "queue_finished only (ADR-0005): the last dispatched expression finished "
    "playing; the input content says how many sentences completed and which PPT "
    "page is showing, plus that page's content description (本页内容) and "
    "suggested speaker script (建议讲稿) so a vision-less Agent sees what the "
    "slide displays and how to teach it; an interrupted variant reports the "
    "queue was cleared. This is the module's "
    "sole input frame - the frontend screen is a pure display surface with no "
    "user-operable elements - and it is the heartbeat that lets her advance the "
    "presentation and decide the next step."
)

CAPABILITY_STATUS_DESCRIPTION: Final = (
    "live presentation state: tts_engine (active engine), playing, queue_depth, "
    "completed_sentences (current round), presentation_available (Godot window "
    "connected), deck/page/total_pages (current PPT state, null when closed), "
    "page_summary (what the shown slide displays) and page_script (suggested "
    "speaker script), both null when the deck carries none"
)

__all__ = [
    "CAPABILITY_INPUT_DESCRIPTION",
    "CAPABILITY_OUTPUT_DESCRIPTION",
    "CAPABILITY_STATUS_DESCRIPTION",
    "EMOTION_TAGS",
    "MODULE_NAME",
    "MOTION_TAGS",
    "OUTPUT_SCHEMA",
]
