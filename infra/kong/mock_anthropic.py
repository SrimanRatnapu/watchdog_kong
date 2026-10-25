"""A stand-in for the Anthropic Messages API, for running Kong and Watchdog offline (no key, no spend).

POST /v1/messages answers with a tool_use block for the first tool in the request. A screening tool gets a low score.
Usage is estimated from the request size (about 4 characters per token), so Kong's token metrics move.
Stdlib only.   python infra/kong/mock_anthropic.py [port]
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CANNED = {"report_screening": {"score": 0.1, "suspicious": False, "reason": "Mock answer from the offline stand-in."}}


def reply(req: dict) -> dict:
    tools = req.get("tools") or []
    name = tools[0]["name"] if tools else None
    content = ([{"type": "tool_use", "id": "toolu_mock", "name": name, "input": CANNED.get(name, {})}] if name
               else [{"type": "text", "text": "mock"}])
    size = len(json.dumps(req))
    return {"id": "msg_mock", "type": "message", "role": "assistant", "model": req.get("model", "mock"),
            "content": content, "stop_reason": "tool_use" if name else "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": max(1, size // 4), "output_tokens": 20,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:   # noqa: N802
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        try:
            out, code = reply(json.loads(body or b"{}")), 200
        except ValueError:
            out, code = {"type": "error", "error": {"type": "invalid_request_error", "message": "bad json"}}, 400
        data = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: object) -> None:
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9999
    print(f"mock Anthropic API on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
