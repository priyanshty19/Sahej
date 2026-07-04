"""
Vercel serverless entry point.

Vercel's Python runtime finds the request handler by static analysis, so it must
see a top-level class literally named `handler` that subclasses
BaseHTTPRequestHandler. Sahej's whole server already is that class
(serve.Handler); here we put the project root on the import path and expose a
thin subclass under the expected name. A catch-all rewrite in vercel.json sends
every path here and the handler routes off self.path exactly as it does locally.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from serve import Handler  # noqa: E402


class handler(Handler):  # noqa: N801 — Vercel requires this exact name
    pass
