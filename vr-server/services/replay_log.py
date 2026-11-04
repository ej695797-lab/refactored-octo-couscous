"""
Replay log lookup helpers.

This module is intentionally conservative: if no replay entry is found (or
replay parsing is not configured yet), it returns None and the normal API
handlers continue.
"""

from __future__ import annotations


def lookup_replay(method: str, path: str, request_body: str):
    """
    Try to find an exact replay match for a request.

    Returns:
        dict with keys: status_code, content_type, response_body
        or None when there is no replay hit.
    """
    # Stub implementation for now. This keeps main.py import/runtime stable.
    _ = (method, path, request_body)
    return None

