"""Small string utilities used by the subagent examples."""

import re


def slugify(text: str) -> str:
    """Convert text into a URL-friendly slug.

    The text is lowercased, every run of non-alphanumeric characters is
    replaced by a single hyphen, and leading/trailing hyphens are stripped.
    """
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
