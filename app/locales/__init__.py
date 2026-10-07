"""Centralised UI texts. Uzbek is the default; add ``ru.py``/``en.py`` with the same keys."""

from __future__ import annotations

import logging

from app.locales import uz

logger = logging.getLogger(__name__)

_CATALOGS: dict[str, dict[str, str]] = {"uz": uz.TEXTS}
DEFAULT_LANG = "uz"


def t(key: str, lang: str | None = None, /, **kwargs: object) -> str:
    catalog = _CATALOGS.get(lang or DEFAULT_LANG, _CATALOGS[DEFAULT_LANG])
    template = catalog.get(key) or _CATALOGS[DEFAULT_LANG].get(key)
    if template is None:
        logger.warning("Missing translation key", extra={"key": key})
        return key
    if kwargs:
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            logger.exception("Bad translation formatting", extra={"key": key})
            return template
    return template


def has_key(key: str) -> bool:
    return key in _CATALOGS[DEFAULT_LANG]
