"""The only module an App script may import besides time, re and typing.

Scripts see it as ``artemis_app_helpers``. Everything here reads the parsed
UI tree; taps go through the ``phone`` object the runner passes in.
"""

from __future__ import annotations

from apps.admin_console.services.screen_intersect import title_matches
from apps.admin_console.services.task_script import (
    area,
    center,
    covers_screen,
    find_dismiss,
    find_exact,
    find_label,
    foreground_packages,
    looks_like_class_name,
    row_action,
    screen_size,
    text_of,
)

MODULE_NAME = "artemis_app_helpers"

__all__ = [
    "area",
    "center",
    "covers_screen",
    "find_dismiss",
    "find_exact",
    "find_label",
    "foreground_packages",
    "looks_like_class_name",
    "row_action",
    "screen_size",
    "text_of",
    "title_matches",
]
