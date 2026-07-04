"""
Vercel serverless entry point.

Vercel's Python runtime looks for a class named `handler` that subclasses
BaseHTTPRequestHandler and hands it each request. Sahej's whole server is that
class already (serve.Handler), so this file is just the glue: put the project
root on the import path, then re-export the handler. A catch-all rewrite in
vercel.json sends every path here; the handler does its own routing off
self.path exactly as it does locally.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from serve import Handler as handler  # noqa: E402,F401
