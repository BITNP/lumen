#!/usr/bin/env python3
"""TEST-ONLY stub of the audiocpp_server TTS service (never a deliverable).

Speaks the real presenter wire format (see presenter.audiocpp) so the host
runs its genuine audio_cpp code path without a model behind it:

- GET  /health              -> 200
- POST /v1/audio/speech     -> SSE speech.audio.delta (base64 s16le mono PCM),
                               speech.audio.done, [DONE]

Every sentence becomes a 5-second, ~330 Hz sine wave at the request's model
rate is irrelevant (the server rate is what the presenter configures,
PRESENTER_AUDIO_CPP_SAMPLE_RATE=44100).  Chunks stream with a small delay to
exercise the real SSE consumption.  Use for the no-TTS-service presenter
e2e test only:

    uv run --locked python tests/stub_tts_server.py --port 8080
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SAMPLE_RATE = 44_100  # matches PRESENTER_AUDIO_CPP_SAMPLE_RATE in .env
DURATION_SECONDS = 5.0
FREQUENCY_HZ = 330.0
AMPLITUDE = 6_000
CHUNK_SECONDS = 0.25
FIRST_CHUNK_DELAY = 0.15

CONTENT_TYPE_SSE = "text/event-stream"


def _sine_pcm16() -> bytes:
    frames = int(SAMPLE_RATE * DURATION_SECONDS)
    return b"".join(
        struct.pack(
            "<h",
            int(AMPLITUDE * math.sin(2 * math.pi * FREQUENCY_HZ * index / SAMPLE_RATE)),
        )
        for index in range(frames)
    )


_SINE = _sine_pcm16()
_CHUNK_BYTES = int(SAMPLE_RATE * CHUNK_SECONDS) * 2


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            body = b'{"status":"stub"}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length) or b"{}")
        if self.path != "/v1/audio/speech":
            self.send_error(404)
            return
        text = str(request.get("input", ""))[:60]
        print(f"[stub-tts] synthesizing {text!r}", flush=True)
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPE_SSE)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        time.sleep(FIRST_CHUNK_DELAY)
        for offset in range(0, len(_SINE), _CHUNK_BYTES):
            chunk = _SINE[offset : offset + _CHUNK_BYTES]
            event = {
                "type": "speech.audio.delta",
                "audio": base64.b64encode(chunk).decode(),
            }
            self._sse_event(event)
            time.sleep(0.02)
        self._sse_event({"type": "speech.audio.done"})
        self._sse_event("[DONE]", raw=True)

    def _sse_event(self, payload: object, *, raw: bool = False) -> None:
        data = payload if raw else json.dumps(payload, ensure_ascii=False)
        self._write_chunk(f"data: {data}\n\n")

    def _write_chunk(self, text: str) -> None:
        body = text.encode("utf-8")
        self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n")
        self.wfile.flush()

    def log_message(self, format: str, *args: object) -> None:  # quiet default
        return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    print(
        f"[stub-tts] TEST-ONLY stub listening on 127.0.0.1:{args.port} "
        f"({DURATION_SECONDS:.0f}s sine @ {SAMPLE_RATE} Hz)",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        threading.main_thread()  # keep mypy quiet, then exit


if __name__ == "__main__":
    main()
