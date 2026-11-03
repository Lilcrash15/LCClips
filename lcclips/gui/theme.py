"""ttk theming for LCClips -- the shared LC-line look (same presets and
widget language as LCBot's chatbot/gui/theme.py): black panels, an orange
accent, flat buttons, accent-colored headings and a themed toolbar strip.

The color globals (BG, FG, ACCENT, ...) are *live*: apply_theme()
reassigns them, so anything reading theme.X at call time follows a theme
switch. ttk widgets restyle themselves automatically; the few plain tk
widgets (the accent border frame, menus) are recolored by the app.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# Identical presets to LCBot so every LC app looks like one family.
PRESETS: dict[str, dict[str, str]] = {
    "classic": {
        "BG": "#1a1a1a", "PANEL_BG": "#202020", "FIELD_BG": "#2a2a2a", "TAB_BG": "#141414",
        "FG": "#e6e6e6", "MUTED_FG": "#9a9a9a", "ACCENT": "#e8720c", "ACCENT_DIM": "#7a3d08",
        "SELECT_BG": "#e8720c", "SELECT_FG": "#141414",
    },
    "dark": {
        "BG": "#181a20", "PANEL_BG": "#1f2128", "FIELD_BG": "#262932", "TAB_BG": "#121317",
        "FG": "#e8e9ec", "MUTED_FG": "#8b8f99", "ACCENT": "#4d9dff", "ACCENT_DIM": "#2a5a92",
        "SELECT_BG": "#4d9dff", "SELECT_FG": "#0d1117",
    },
    "light": {
        "BG": "#f2f2f4", "PANEL_BG": "#ffffff", "FIELD_BG": "#ffffff", "TAB_BG": "#e3e3e7",
        "FG": "#1c1c1e", "MUTED_FG": "#68686d", "ACCENT": "#d9640a", "ACCENT_DIM": "#c96410",
        "SELECT_BG": "#d9640a", "SELECT_FG": "#ffffff",
    },
    "synthwave": {
        "BG": "#1a0f2e", "PANEL_BG": "#241640", "FIELD_BG": "#2e1c52", "TAB_BG": "#150a24",
        "FG": "#f2e9ff", "MUTED_FG": "#a992c9", "ACCENT": "#ff2e97", "ACCENT_DIM": "#9c1a5c",
        "SELECT_BG": "#ff2e97", "SELECT_FG": "#1a0f2e",
    },
    "forest": {
        "BG": "#0f1c14", "PANEL_BG": "#16261c", "FIELD_BG": "#1e3327", "TAB_BG": "#0a140e",
        "FG": "#dcecdf", "MUTED_FG": "#8fae97", "ACCENT": "#4caf50", "ACCENT_DIM": "#2e6b31",
        "SELECT_BG": "#4caf50", "SELECT_FG": "#0f1c14",
    },
}
THEME_ORDER = ["classic", "dark", "light", "synthwave", "forest"]
THEME_LABELS = {
    "classic": "Classic (LC Orange)",
    "dark": "Dark Mode",
    "light": "Light Mode",
    "synthwave": "Synthwave",
    "forest": "Forest",
}

REC_RED = "#e5484d"
SUCCESS = "#5cb85c"
FONT = "Segoe UI"

BG = PRESETS["classic"]["BG"]
PANEL_BG = PRESETS["classic"]["PANEL_BG"]
FIELD_BG = PRESETS["classic"]["FIELD_BG"]
TAB_BG = PRESETS["classic"]["TAB_BG"]
FG = PRESETS["classic"]["FG"]
MUTED_FG = PRESETS["classic"]["MUTED_FG"]
ACCENT = PRESETS["classic"]["ACCENT"]
ACCENT_DIM = PRESETS["classic"]["ACCENT_DIM"]
SELECT_BG = PRESETS["classic"]["SELECT_BG"]
SELECT_FG = PRESETS["classic"]["SELECT_FG"]


def resolve_colors(theme_name: str) -> dict[str, str]:
    return dict(PRESETS.get(theme_name, PRESETS["classic"]))


def _luminance(color: str) -> float:
    c = color.lstrip("#")
    r, g, b = (int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def current_is_dark() -> bool:
    return _luminance(BG) < 0.5


def apply_theme(root: tk.Tk, colors: dict[str, str]) -> ttk.Style:
    global BG, PANEL_BG, FIELD_BG, TAB_BG, FG, MUTED_FG, ACCENT, ACCENT_DIM, SELECT_BG, SELECT_FG
    BG, PANEL_BG, FIELD_BG, TAB_BG = colors["BG"], colors["PANEL_BG"], colors["FIELD_BG"], colors["TAB_BG"]
    FG, MUTED_FG = colors["FG"], colors["MUTED_FG"]
    ACCENT, ACCENT_DIM = colors["ACCENT"], colors["ACCENT_DIM"]
    SELECT_BG, SELECT_FG = colors["SELECT_BG"], colors["SELECT_FG"]

    root.configure(bg=BG)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(".", background=BG, foreground=FG, fieldbackground=FIELD_BG,
                    bordercolor=ACCENT_DIM, lightcolor=BG, darkcolor=BG,
                    insertcolor=FG, font=(FONT, 9))
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=FG)
    style.configure("Muted.TLabel", background=BG, foreground=MUTED_FG)
    style.configure("Success.TLabel", background=BG, foreground=SUCCESS, font=(FONT, 9, "bold"))
    style.configure("UpdateAvailable.TLabel", background=BG, foreground=ACCENT, font=(FONT, 9, "bold", "underline"))
    style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=FIELD_BG, bordercolor=ACCENT_DIM,
                    lightcolor=ACCENT, darkcolor=ACCENT)
    style.configure("Error.TLabel", background=BG, foreground=REC_RED)
    style.configure("Heading.TLabel", background=BG, foreground=ACCENT, font=(FONT, 10, "bold"))
    style.configure("Rec.TLabel", background=BG, foreground=REC_RED, font=(FONT, 9, "bold"))
    style.configure("RecOff.TLabel", background=BG, foreground=MUTED_FG, font=(FONT, 9, "bold"))

    # Cards (recent-clip tiles, details panel): one step "raised" from BG.
    style.configure("Card.TFrame", background=PANEL_BG)
    style.configure("Card.TLabel", background=PANEL_BG, foreground=FG)
    style.configure("CardTitle.TLabel", background=PANEL_BG, foreground=FG, font=(FONT, 11, "bold"))
    style.configure("CardMuted.TLabel", background=PANEL_BG, foreground=MUTED_FG)
    style.configure("CardLink.TLabel", background=PANEL_BG, foreground=ACCENT, font=(FONT, 9, "underline"))
    style.configure("Thumb.TLabel", background="#000000", foreground=MUTED_FG, anchor="center")
    style.configure("Preview.TLabel", background=PANEL_BG, anchor="center")
    style.configure("SelectedCard.TFrame", background=ACCENT)

    # Flat buttons -- same hover/pressed language as LCBot.
    style.configure("TButton", background=FIELD_BG, foreground=FG, borderwidth=0,
                    relief="flat", focuscolor=ACCENT, padding=(14, 8))
    style.map("TButton",
              background=[("disabled", PANEL_BG), ("pressed", ACCENT_DIM), ("active", ACCENT)],
              foreground=[("disabled", MUTED_FG), ("pressed", SELECT_FG), ("active", SELECT_FG)])
    style.configure("Accent.TButton", background=ACCENT, foreground=SELECT_FG, borderwidth=0,
                    relief="flat", focuscolor=ACCENT, padding=(16, 8), font=(FONT, 9, "bold"))
    style.map("Accent.TButton",
              background=[("disabled", ACCENT_DIM), ("pressed", ACCENT_DIM), ("active", FG)],
              foreground=[("active", BG)])
    style.configure("Icon.TButton", background=FIELD_BG, foreground=FG, borderwidth=0,
                    relief="flat", focuscolor=ACCENT, padding=(8, 4), font=(FONT, 13, "bold"))
    style.map("Icon.TButton",
              background=[("pressed", ACCENT_DIM), ("active", ACCENT)],
              foreground=[("pressed", SELECT_FG), ("active", SELECT_FG)])

    style.configure("TCheckbutton", background=BG, foreground=FG, indicatorbackground=FIELD_BG,
                    indicatorforeground=SELECT_FG, indicatormargin=(0, 0, 6, 0), bordercolor=ACCENT_DIM)
    style.map("TCheckbutton", background=[("active", BG)], foreground=[("active", ACCENT)],
              indicatorbackground=[("selected", ACCENT), ("active", FIELD_BG)])
    style.configure("TRadiobutton", background=BG, foreground=FG)
    style.map("TRadiobutton", background=[("active", BG)], foreground=[("active", ACCENT)])

    style.configure("TEntry", fieldbackground=FIELD_BG, foreground=FG, insertcolor=FG,
                    bordercolor=ACCENT_DIM, padding=(8, 6))
    style.map("TEntry", bordercolor=[("focus", ACCENT), ("!focus", ACCENT_DIM)])
    style.configure("TSpinbox", fieldbackground=FIELD_BG, foreground=FG, arrowsize=12,
                    bordercolor=ACCENT_DIM, padding=(8, 6), arrowcolor=FG, background=FIELD_BG)
    style.map("TSpinbox", bordercolor=[("focus", ACCENT), ("!focus", ACCENT_DIM)])
    style.configure("TCombobox", fieldbackground=FIELD_BG, background=FIELD_BG, foreground=FG,
                    arrowsize=12, bordercolor=ACCENT_DIM, padding=(8, 6), arrowcolor=FG)
    style.map("TCombobox", fieldbackground=[("readonly", FIELD_BG)], foreground=[("readonly", FG)],
              bordercolor=[("focus", ACCENT), ("!focus", ACCENT_DIM)])
    root.option_add("*TCombobox*Listbox.background", FIELD_BG)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", SELECT_FG)

    style.configure("Horizontal.TScale", background=BG, troughcolor=FIELD_BG, bordercolor=ACCENT_DIM)

    style.configure("TLabelframe", background=BG, bordercolor=ACCENT_DIM)
    style.configure("TLabelframe.Label", background=BG, foreground=ACCENT, font=(FONT, 9, "bold"))

    style.configure("TNotebook", background=BG, bordercolor=ACCENT, borderwidth=2, tabmargins=(2, 4, 2, 0))
    style.configure("TNotebook.Tab", background=TAB_BG, foreground=FG, padding=(12, 5), borderwidth=0,
                    font=(FONT, 9))
    style.map("TNotebook.Tab",
              background=[("selected", BG)],
              foreground=[("selected", ACCENT)],
              expand=[("selected", (1, 1, 1, 0))])

    style.configure("Treeview", background=PANEL_BG, fieldbackground=PANEL_BG, foreground=FG,
                    bordercolor=ACCENT_DIM, borderwidth=1, rowheight=28)
    style.configure("Treeview.Heading", background=TAB_BG, foreground=ACCENT, relief="flat",
                    font=(FONT, 9, "bold"), padding=(6, 4))
    style.map("Treeview.Heading", background=[("active", ACCENT_DIM)])
    style.map("Treeview", background=[("selected", SELECT_BG)], foreground=[("selected", SELECT_FG)])

    for sb in ("TScrollbar", "Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(sb, background=FIELD_BG, troughcolor=PANEL_BG, bordercolor=PANEL_BG,
                        lightcolor=FIELD_BG, darkcolor=FIELD_BG, arrowcolor=MUTED_FG, gripcount=0)
        style.map(sb, background=[("active", ACCENT_DIM), ("pressed", ACCENT)])
    style.configure("TSeparator", background=ACCENT_DIM)

    style.configure("Toolbar.TFrame", background=TAB_BG)
    style.configure("Toolbar.TLabel", background=TAB_BG, foreground=MUTED_FG)
    style.configure("Toolbar.TMenubutton", background=TAB_BG, foreground=FG, arrowcolor=FG,
                    borderwidth=0, relief="flat", padding=(10, 4), font=(FONT, 9))
    style.map("Toolbar.TMenubutton",
              background=[("active", ACCENT_DIM), ("pressed", ACCENT_DIM)],
              foreground=[("active", FG)])
    return style


def popup_menu_kwargs() -> dict:
    return dict(bg=FIELD_BG, fg=FG, activebackground=ACCENT, activeforeground=SELECT_FG,
                disabledforeground=MUTED_FG, relief="flat", borderwidth=1, activeborderwidth=0)
