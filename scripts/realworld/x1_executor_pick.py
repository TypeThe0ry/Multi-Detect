#!/usr/bin/env python3
"""Print the first X1 executor that passes a lightweight CLI check."""

import argparse
import json
import os
import re
import subprocess
import sys

AGY_MODELS = ("claude-sonnet-5-5-high", "gemini-4-neon-high")
CODEX_MODEL = "gpt-6.2-sol"
CHECK_TIMEOUT = 8


def check(*argv: str) -> tuple[str | None, str]:
    """Return stdout on success or a fixed failure reason without CLI output."""
    try:
        result = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            errors="replace", timeout=CHECK_TIMEOUT,
        )
    except FileNotFoundError:
        return None, "CLI not found on PATH"
    except subprocess.TimeoutExpired:
        return None, f"check timed out after {CHECK_TIMEOUT}s"
    except OSError:
        return None, "CLI could not start"

    if result.returncode == 0:
        return result.stdout, ""

    output = (result.stdout + "\n" + result.stderr).lower()
    if re.search(r"not logged in|login required|not authenticated|unauthenticated|unauthorized", output):
        return None, "authentication required or rejected"
    if re.search(r"quota exceeded|quota exhausted|rate limit", output):
        return None, "quota or rate limit reported by CLI"
    if re.search(r"operation not permitted|permission denied|read-only file system", output):
        return None, "check blocked by environment permissions"
    return None, f"check failed (exit {result.returncode})"


def gemini_model_ids() -> tuple[set[str], str]:
    """Ask the separate Gemini CLI for an ACP model catalog without submitting a prompt."""
    argv = ("gemini", "--acp", "--extensions", "none")
    messages = ({"id": 1, "method": "initialize", "params": {"protocolVersion": 1, "clientCapabilities": {}}},
                {"id": 2, "method": "session/new", "params": {"cwd": os.getcwd(), "mcpServers": []}})
    request = "".join(json.dumps({"jsonrpc": "2.0", **row}) + "\n" for row in messages)
    timed_out = False
    try:
        response = subprocess.run(argv, input=request, capture_output=True, text=True,
                                  errors="replace", timeout=CHECK_TIMEOUT).stdout
    except FileNotFoundError:
        return set(), "CLI not found on PATH"
    except OSError:
        return set(), "CLI could not start"
    except subprocess.TimeoutExpired as exc:
        response = exc.stdout or b""
        if isinstance(response, bytes):
            response = response.decode(errors="replace")
        timed_out = True
    for line in response.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("id") != 2:
            continue
        if isinstance(row.get("error"), dict):
            message = str(row["error"].get("message", ""))
            if re.search(r"authenticat|api key|sign in", message, re.IGNORECASE):
                return set(), "authentication required or rejected"
            return set(), "ACP session could not list models"
        catalog = (row.get("result") or {}).get("models") or {}
        return {item["modelId"] for item in catalog.get("availableModels", [])
                if isinstance(item, dict) and isinstance(item.get("modelId"), str)}, "exact ID absent from Gemini catalog"
    return set(), f"check timed out after {CHECK_TIMEOUT}s" if timed_out else "ACP model catalog unavailable"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Prints one backend:model line on stdout or exits 1 with empty stdout. "
        "Checks use agy models, the Gemini ACP catalog, and codex login status. Model requests, quota and task "
        "execution are outside these checks.",
    )
    parser.parse_args(argv)

    # Both first choices use agy. Query the catalog once per invocation.
    catalog, failure = check("agy", "models")
    listed_ids = set(re.findall(r"[\w.-]+", catalog or ""))
    for model in AGY_MODELS:
        candidate = f"agy:{model}"
        if model in listed_ids:
            print(f"{candidate}: exact ID listed by agy models", file=sys.stderr)
            print(candidate)
            return 0
        print(f"{candidate}: {failure or 'exact ID absent from agy models'}", file=sys.stderr)

    model = AGY_MODELS[1]
    models, failure = gemini_model_ids()
    if model in models:
        print(f"gemini:{model}: exact ID listed by Gemini ACP", file=sys.stderr)
        print(f"gemini:{model}")
        return 0
    print(f"gemini:{model}: {failure}", file=sys.stderr)

    candidate = f"codex:{CODEX_MODEL}"
    status, failure = check("codex", "login", "status")
    if status is not None:
        print(f"{candidate}: login status passed, model availability unverified", file=sys.stderr)
        print(candidate)
        return 0

    print(f"{candidate}: {failure}", file=sys.stderr)
    print("No X1 executor passed its check.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
