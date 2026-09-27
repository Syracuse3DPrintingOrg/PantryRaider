"""Map the active web UI theme onto Stream Deck key colours.

The deck should look like the app it drives (FoodAssistant-gxl). Each action
has a semantic role (a status count, a trigger, a nav key, a timer, and so on);
each UI theme defines a small palette keyed by those roles. ``themed_color``
looks up an action's role in the active theme's palette and returns a colour,
falling back to the action's own default when the theme or role is unknown.

The default ``dark`` theme is intentionally absent from ``THEME_PALETTES`` so it
keeps the hand-tuned per-action colours shipped in actions.py; only the other
themes recolour the deck to match their accent family. Kept free of any
hardware or Pillow import so it is cheap to unit-test.
"""
from __future__ import annotations

# Semantic role for each built-in action. Dynamic action names that carry a
# numeric suffix (ha_1..ha_5, timer_1..timer_3, keypad_*) are matched by prefix
# in role_of(), so only the base names need listing here.
ROLE_BY_ACTION: dict[str, str] = {
    "expiring": "warn",
    "pending": "primary",
    "commit": "success",
    "add": "accent",
    "inventory": "accent",
    "cook": "accent",
    "recipes": "accent",
    "mealplan": "accent",
    "shopping": "accent",
    "defaults": "accent",
    "brightness": "muted",
    "page_next": "muted",
    "page_prev": "muted",
    "pin": "primary",
    "weather": "info",
    "forecast": "info",
}

# Prefix -> role for the suffixed action families.
_ROLE_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("timer_", "timer"),
    ("ha_", "accent"),
    ("keypad_", "muted"),
)

# Accent palettes echoing each vendored Bootswatch (or native) theme. Keys are
# the roles in ROLE_BY_ACTION. "dark" is omitted on purpose so it keeps the
# default deck colours; everything else recolours to match the web theme.
THEME_PALETTES: dict[str, dict[str, str]] = {
    "light": {
        "primary": "#0d6efd", "success": "#198754", "warn": "#fd7e14",
        "danger": "#dc3545", "info": "#0dcaf0", "accent": "#6f42c1",
        "timer": "#20c997", "muted": "#6c757d",
    },
    "darkly": {
        "primary": "#375a7f", "success": "#00bc8c", "warn": "#f39c12",
        "danger": "#e74c3c", "info": "#3498db", "accent": "#2c3e50",
        "timer": "#00bc8c", "muted": "#444444",
    },
    "cyborg": {
        "primary": "#2a9fd6", "success": "#77b300", "warn": "#ff8800",
        "danger": "#cc0000", "info": "#9933cc", "accent": "#2a9fd6",
        "timer": "#77b300", "muted": "#282828",
    },
    "flatly": {
        "primary": "#2c3e50", "success": "#18bc9c", "warn": "#f39c12",
        "danger": "#e74c3c", "info": "#3498db", "accent": "#18bc9c",
        "timer": "#18bc9c", "muted": "#95a5a6",
    },
    "synthwave": {
        "primary": "#f92aad", "success": "#36f9c7", "warn": "#ff8b39",
        "danger": "#fe4450", "info": "#03edf9", "accent": "#b893ce",
        "timer": "#36f9c7", "muted": "#241b2f",
    },
    "solarized": {
        "primary": "#268bd2", "success": "#859900", "warn": "#b58900",
        "danger": "#dc322f", "info": "#6c71c4", "accent": "#2aa198",
        "timer": "#2aa198", "muted": "#93a1a1",
    },
    "midnight": {
        "primary": "#4f9dff", "success": "#34d399", "warn": "#fbbf24",
        "danger": "#f87171", "info": "#a78bfa", "accent": "#38bdf8",
        "timer": "#34d399", "muted": "#1f2937",
    },
    "forest": {
        "primary": "#5fb872", "success": "#9ccc5a", "warn": "#e0a93b",
        "danger": "#c2502e", "info": "#6fae9b", "accent": "#8fbf9f",
        "timer": "#9ccc5a", "muted": "#2a4533",
    },
    "ios-light": {
        "primary": "#007aff", "success": "#248a3d", "warn": "#b25000",
        "danger": "#d70015", "info": "#5856d6", "accent": "#007aff",
        "timer": "#248a3d", "muted": "#d1d1d6",
    },
    "ios-dark": {
        "primary": "#0a84ff", "success": "#30d158", "warn": "#ff9f0a",
        "danger": "#ff453a", "info": "#5e5ce6", "accent": "#0a84ff",
        "timer": "#30d158", "muted": "#38383a",
    },
    "outrun": {
        "primary": "#ff2e97", "success": "#29ffc6", "warn": "#ffd23f",
        "danger": "#ff3864", "info": "#9d4edd", "accent": "#2de2e6",
        "timer": "#29ffc6", "muted": "#34306b",
    },
    "vaporwave": {
        "primary": "#ff6ad5", "success": "#94f0c2", "warn": "#fff07c",
        "danger": "#ff5d8f", "info": "#c774e8", "accent": "#5ce1e6",
        "timer": "#94f0c2", "muted": "#463473",
    },
    # Custom theme builder (FoodAssistant-hatd). The web UI lets a user pick their
    # own primary/accent/surface colours (Settings -> Interface). The deck reads
    # this table locally and only receives the theme NAME via the satellite sync,
    # not the live custom colours, so this is a sensible STATIC palette derived
    # from the app's default custom-theme swatches (slate dark with a blue primary
    # and green accent). Pushing the live custom colours all the way to the deck
    # is a follow-up (it would need the sync to carry the custom_theme_* values
    # and the controller to build a palette from them at runtime).
    "custom": {
        "primary": "#4f9dff", "success": "#34d399", "warn": "#fbbf24",
        "danger": "#f87171", "info": "#a78bfa", "accent": "#34d399",
        "timer": "#34d399", "muted": "#161b22",
    },
}


# Near-black and near-white label colours. Not pure black/white so the text
# keeps a hint of softness against a flat key background.
_DARK_TEXT = "#1a1a1a"
_LIGHT_TEXT = "#ebebeb"


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    """Parse a ``#rrggbb`` colour to an (r, g, b) tuple of 0..255 ints.

    Falls back to a mid grey when the value is not a six-digit hex string, so
    the contrast helper never raises on a malformed colour.
    """
    v = value.lstrip("#")
    if len(v) != 6:
        return (60, 60, 60)
    try:
        return tuple(int(v[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return (60, 60, 60)


def relative_luminance(bg_hex: str) -> float:
    """Standard sRGB relative luminance of a colour, in 0.0..1.0.

    Linearises each channel per the WCAG definition, then weights them by human
    sensitivity (green most, blue least). Used to decide whether a key wants
    dark or light label text.
    """
    def _channel(c: int) -> float:
        s = c / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (_channel(c) for c in _hex_to_rgb(bg_hex))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def text_color_for(bg_hex: str) -> str:
    """Pick a readable label colour for a key painted ``bg_hex``.

    Returns near-black on light backgrounds and near-white on dark ones, using a
    WCAG-style luminance threshold so contrast stays adequate across every theme
    and role (for example, white text on a light green Commit key was the bug
    this fixes).
    """
    return _DARK_TEXT if relative_luminance(bg_hex) > 0.5 else _LIGHT_TEXT


# Vivid accent colour per semantic role, used when a key is drawn with full
# colour icons (render.icon_color == "full"). These are deliberately brighter
# and more saturated than the key backgrounds so a glyph painted in the role's
# accent pops against the (darker) key face. role_accent() falls back to a
# bright neutral for any role not listed here.
_ROLE_ACCENT: dict[str, str] = {
    "warn": "#fb923c",
    "primary": "#60a5fa",
    "success": "#4ade80",
    "danger": "#f87171",
    "info": "#38bdf8",
    "accent": "#c084fc",
    "timer": "#2dd4bf",
    "muted": "#cbd5e1",
}

# A bright neutral for any glyph whose role has no specific accent.
_ACCENT_FALLBACK = "#e2e8f0"


def role_accent(action_name: str, fallback: str = _ACCENT_FALLBACK) -> str:
    """Vivid icon-accent colour for an action, or ``fallback`` if it has none.

    Pure lookup used by the renderer's full-colour icon mode. The colour is the
    role's accent, not the key background, so the glyph reads as a brighter tint
    of the same colour family. Callers still guard legibility against the key
    background luminance before using it.
    """
    role = role_of(action_name)
    if role is None:
        return fallback
    return _ROLE_ACCENT.get(role, fallback)


def role_of(action_name: str) -> str | None:
    """Semantic role for an action name, or None if it has no themed role."""
    if action_name in ROLE_BY_ACTION:
        return ROLE_BY_ACTION[action_name]
    for prefix, role in _ROLE_BY_PREFIX:
        if action_name.startswith(prefix):
            return role
    return None


def themed_color(action_name: str, fallback: str, theme: str) -> str:
    """Colour for ``action_name`` under ``theme``, or ``fallback``.

    Falls back to the action's own colour when the theme is the default/unknown
    or the action has no themed role, so callers can pass ``spec.color`` and get
    the existing behaviour whenever theming does not apply.
    """
    palette = THEME_PALETTES.get(theme)
    if not palette:
        return fallback
    role = role_of(action_name)
    if role is None:
        return fallback
    return palette.get(role, fallback)
