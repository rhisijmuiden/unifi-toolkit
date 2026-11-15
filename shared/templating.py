"""
Jinja2 templates with the shared app shell (brand head + left sidebar).

Every page (dashboard and each tool) gets its templates from make_templates(),
so `{% include "_brand_head.html" %}` and `{% include "_sidebar.html" %}`
resolve from app/templates/shared no matter which tool renders the page.
"""
import os
from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader

SHARED_TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates" / "shared"

# The tools, in sidebar order: (key, label, url)
NAV_ITEMS = [
    ("dashboard", "Dashboard", "/"),
    ("stalker", "Wi-Fi Stalker", "/stalker/"),
    ("threats", "Threat Watch", "/threats/"),
    ("pulse", "Network Pulse", "/pulse/"),
    ("arrest", "House Arrest", "/arrest/"),
]

# System administration pages, shown as a second group: (key, label, url)
SYSTEM_NAV_ITEMS = [
    ("alerts", "Alerts", "/system/alerts"),
    ("mail", "Mail server", "/system/mail"),
    ("backups", "Backups", "/system/backups"),
]


def _auth_enabled() -> bool:
    return os.getenv("DEPLOYMENT_TYPE", "local").lower() == "production"


def make_templates(directory) -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(directory))
    templates.env.loader = ChoiceLoader([
        templates.env.loader,
        FileSystemLoader(str(SHARED_TEMPLATES)),
    ])
    templates.env.globals["nav_items"] = NAV_ITEMS
    templates.env.globals["system_nav_items"] = SYSTEM_NAV_ITEMS
    templates.env.globals["shell_auth_enabled"] = _auth_enabled
    return templates
