"""Settings persistence for ac_chapp (~/.config/ac_chapp/settings.json)."""

import json
import os

_SETTINGS_DIR = os.path.join(os.path.expanduser("~"), ".config", "ac_chapp")
_SETTINGS_FILE = os.path.join(_SETTINGS_DIR, "settings.json")

DEFAULT_FONT_SIZES = {
    "base": 13,
    "stderr": 8,
    "title_bar": 12,
    "settings_label": 13,
    "settings_input": 13,
    "settings_button": 12,
}


def read_font_sizes() -> dict:
    """Read font sizes from settings, merging with defaults."""
    settings = read_settings()
    saved = settings.get("font_sizes", {})
    result = dict(DEFAULT_FONT_SIZES)
    result.update(saved)
    return result


def read_settings() -> dict:
    """Read settings from ~/.config/ac_chapp/settings.json."""
    if not os.path.exists(_SETTINGS_FILE):
        return {}
    try:
        with open(_SETTINGS_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_settings(data: dict) -> None:
    """Save settings to ~/.config/ac_chapp/settings.json."""
    os.makedirs(_SETTINGS_DIR, exist_ok=True)
    with open(_SETTINGS_FILE, "w") as f:
        json.dump(data, f)
