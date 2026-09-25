#!/usr/bin/env python3
"""Read what real speakers say, and print it as one JSON envelope. GET ONLY.

This runs on a machine that can reach the speakers; the development host cannot, which is why it
is shipped there and run rather than driven step by step over ssh. It is stdlib-only on purpose,
so the far end needs a python3 and nothing else: no installer, no index, no first-run fetch.

Every request here is a GET. That is the whole safety argument, and it is structural rather than
careful: this file contains no POST, so it cannot select a source, change a volume, or power a
speaker on. Reading /info from the Lifestyle console is as harmless as reading it from any other
box; it is a POST that would flip its input.

The envelope matches the other tools in this repository: {ok, command, data, skipped}. Exit 0 when
every speaker answered, 1 when one ran but did not, 2 when it could not run at all.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request

PORT = 8090
PATHS = ("/info", "/now_playing", "/volume", "/presets", "/getZone")


def fetch(ip: str, path: str, timeout: float) -> str:
    """One GET, as text. The speaker answers XML; it is returned raw so the caller can parse it."""
    with urllib.request.urlopen(f"http://{ip}:{PORT}{path}", timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def read_speaker(ip: str, timeout: float) -> dict[str, str]:
    """Every document one speaker serves, keyed by its path."""
    return {path: fetch(ip, path, timeout) for path in PATHS}


def main(argv: list[str] | None = None) -> int:
    """Probe each speaker in turn; one that does not answer is skipped, not fatal."""
    parser = argparse.ArgumentParser(description="Read /info, /now_playing, /volume, /presets and /getZone.")
    parser.add_argument("--speakers", required=True, help="comma separated IP addresses")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args(argv)

    ips = [part.strip() for part in str(args.speakers).split(",") if part.strip()]
    if not ips:
        print(json.dumps({"ok": False, "command": "speaker_probe", "error": "ValueError", "message": "no speakers"}))
        return 2

    data: dict[str, dict[str, str]] = {}
    skipped: list[str] = []
    for ip in ips:
        try:
            data[ip] = read_speaker(ip, float(args.timeout))
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            skipped.append(f"{ip}: {exc!r}")
    print(json.dumps({"ok": not skipped, "command": "speaker_probe", "data": {"speakers": data}, "skipped": skipped}))
    return 0 if not skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
