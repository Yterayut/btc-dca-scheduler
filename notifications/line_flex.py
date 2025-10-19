"""Helpers for building LINE Flex messages used by notification module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

FlexThemeName = Literal["default", "warning", "success", "info", "danger"]


@dataclass(frozen=True)
class FlexTheme:
    header_color: str
    accent_color: str
    text_color: str
    background_color: str = "#0e1116"


THEMES: dict[FlexThemeName, FlexTheme] = {
    "default": FlexTheme(header_color="#1B9CFC", accent_color="#F8C102", text_color="#FFFFFF"),
    "warning": FlexTheme(header_color="#F2994A", accent_color="#F2C94C", text_color="#FFFFFF"),
    "success": FlexTheme(header_color="#27AE60", accent_color="#6FCF97", text_color="#FFFFFF"),
    "info": FlexTheme(header_color="#3D7BF7", accent_color="#56CCF2", text_color="#FFFFFF"),
    "danger": FlexTheme(header_color="#B71C1C", accent_color="#FF5252", text_color="#FFFFFF"),
}


def _text(text: str, weight: str = "regular", size: str = "sm", color: str | None = None) -> dict:
    node = {"type": "text", "text": text, "size": size, "wrap": True}
    if weight != "regular":
        node["weight"] = weight
    if color:
        node["color"] = color
    return node


def _kv_row(label: str, value: str, theme: FlexTheme) -> dict:
    return {
        "type": "box",
        "layout": "baseline",
        "spacing": "sm",
        "contents": [
            _text(label, weight="bold", color=theme.accent_color),
            _text(value, size="sm", color=theme.text_color),
        ],
    }


def _divider(color: str) -> dict:
    return {"type": "separator", "color": color, "margin": "md"}


def build_basic_bubble(
    title: str,
    sections: Iterable[tuple[str, str]],
    *,
    subtitle: str | None = None,
    theme: FlexThemeName = "default",
    footer_note: str | None = None,
) -> dict:
    scheme = THEMES[theme]
    body_contents: list[dict] = [
        _text(title, weight="bold", size="lg", color=scheme.accent_color),
    ]
    if subtitle:
        body_contents.append(_text(subtitle, size="xs", color="#C8D6E5"))
        body_contents.append(_divider("#1f2833"))

    for label, value in sections:
        body_contents.append(_kv_row(label, value, scheme))

    if footer_note:
        body_contents.append(_divider("#1f2833"))
        body_contents.append(_text(footer_note, size="xs", color="#C8D6E5"))

    return {
        "type": "bubble",
        "size": "mega",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "backgroundColor": scheme.background_color,
            "paddingAll": "16px",
            "contents": body_contents,
        },
        "styles": {
            "body": {"backgroundColor": scheme.background_color},
            "header": {"backgroundColor": scheme.header_color},
        },
    }


def make_flex_message(alt_text: str, bubble: dict) -> dict:
    """Wrap bubble dict into a LINE Flex message envelope."""
    return {
        "type": "flex",
        "altText": alt_text[:400],  # LINE limit
        "contents": bubble,
    }
