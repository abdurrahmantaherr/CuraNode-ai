"""Filesystem layout.

Every directory the application needs is derived here rather than counting
`.parent` hops at each call site, so moving a package cannot silently break a
template lookup.

    <root>/
      backend/app/   <- this package
      frontend/      <- templates and static assets
"""

from __future__ import annotations

from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
BACKEND_DIR = APP_DIR.parent
PROJECT_ROOT = BACKEND_DIR.parent

FRONTEND_DIR = PROJECT_ROOT / "frontend"
TEMPLATES_DIR = FRONTEND_DIR / "templates"
STATIC_DIR = FRONTEND_DIR / "static"
