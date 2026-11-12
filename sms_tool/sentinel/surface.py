"""Candidate sets for the SDK's per-page randomized environment probes.

Why this module exists
----------------------
``sdk.js getConfig()`` builds the 25-tuple that becomes the ``p`` field of every
Sentinel proof.  Three of those slots are **deliberately random per page load**:

======  =========================================================================
``[10]``  一个 ``Object.keys(Object.getPrototypeOf(navigator))`` 键 + ``"−"`` + 该键值的 ``toString()``
``[11]``  一个 ``Object.keys(document)`` 键
``[12]``  一个 ``Object.keys(window)`` 键
======  =========================================================================

The protocol lane cannot run the SDK to fetch the challenge (the requirements
token is what the challenge is *fetched with*), so it hand-builds the same
25-tuple in :func:`sms_tool.sentinel.client._requirements_token`.  Hand-building
those three slots with constants is what this module fixes: on 2026-11-12 all
three were literal strings, so **every account of every batch sent the same
triplet**.  A real browser repeats a triplet with probability ~1/(83·304·263)
≈ 1.5e-7, so one server-side comparison of ``p[10]`` against a known constant
would have grouped the entire fleet for free.

Two invariants make the sampling safe
-------------------------------------
1. **Candidates are real.**  Every navigator candidate is a key that a real
   Chrome navigator exposes *and* that the vendored shim exposes, with the exact
   rendering the shim produces; document and window candidates are the
   intersection of the shim's own keys with a real-browser capture
   (``turb-gpt-free-register``'s ``sentinel/env-surface.json``, captured
   2026-11-12).  Sampling a key the VM does not have would produce a value no
   browser can produce, which is worse than a constant.
2. **Values are byte-exact.**  ``[10]`` embeds ``String(navigator[key])``, so the
   rendering for each key is pinned here as a literal and asserted against the
   live VM by ``tests/registration/test_sentinel_proof_parity.py``.  Four keys
   used to render ``"[object Object]"`` in the shim (``geolocation`` /
   ``mediaDevices`` / ``permissions`` / ``storage``); the missing
   ``Symbol.toStringTag`` was added to the runner rather than excluded here, so
   the *SDK's own* proof stopped emitting an impossible value as well.

Determinism is per account, not per call: the triplet is derived from the
account's device seed, so one account keeps one page identity across its
sub-flows (the same choice ``fingerprint_pool`` and ``tls_fingerprint`` make),
while two accounts draw independently.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

#: ``"−"`` (U+2212 MINUS SIGN) — the character ``sdk.js`` interpolates between
#: the navigator key and its value in ``p[10]``.  Not a hyphen: the payload
#: travels as JSON and the byte is compared verbatim.
SEPARATOR = "\u2212"

#: Navigator candidates whose rendering is a constant, mapped to exactly what
#: ``String(navigator[key])`` returns in the shim.  ``None`` means the value is
#: ``undefined``, where the SDK's ``try`` around ``.toString()`` fails and the
#: probe degenerates to the bare key (``p[10] == "webdriver"``).
NAVIGATOR_PROBES: Mapping[str, str | None] = {
    "bluetooth": "[object Bluetooth]",
    "connection": "[object NetworkInformation]",
    "cookieEnabled": "true",
    "doNotTrack": "null",
    "geolocation": "[object Geolocation]",
    "getGamepads": "function getGamepads() { [native code] }",
    "javaEnabled": "function javaEnabled() { [native code] }",
    "mediaDevices": "[object MediaDevices]",
    "mimeTypes": "[object MimeTypeArray]",
    "onLine": "true",
    "pdfViewerEnabled": "true",
    "permissions": "[object Permissions]",
    "plugins": "[object PluginArray]",
    "sendBeacon": "function sendBeacon() { [native code] }",
    "storage": "[object StorageManager]",
    "webdriver": None,
}

#: Navigator candidates whose value comes from the account's own fingerprint,
#: mapped to the ``sentinel_fingerprint()`` field that supplies it.  These are
#: the same values the HTTP headers and the shim's ``navigator`` carry, so the
#: probe can never disagree with them.
NAVIGATOR_PROFILE_PROBES: Mapping[str, str] = {
    "hardwareConcurrency": "hardware_concurrency",
    "language": "lang",
    "languages": "lang_full",
    "maxTouchPoints": "max_touch_points",
    "platform": "navigator_platform",
    "userAgent": "user_agent",
    "vendor": "navigator_vendor",
}

#: ``Object.keys(document)`` candidates: shim ∩ real Chrome capture.
DOCUMENT_PROBES: tuple[str, ...] = (
    "URL",
    "addEventListener",
    "body",
    "characterSet",
    "charset",
    "compatMode",
    "contentType",
    "cookie",
    "createElement",
    "createTextNode",
    "currentScript",
    "defaultView",
    "dispatchEvent",
    "documentElement",
    "documentURI",
    "getElementById",
    "getElementsByTagName",
    "hasFocus",
    "head",
    "hidden",
    "location",
    "querySelector",
    "querySelectorAll",
    "readyState",
    "referrer",
    "removeEventListener",
    "scripts",
    "title",
    "visibilityState",
)

#: ``Object.keys(window)`` candidates: shim ∩ real Chrome capture.
WINDOW_PROBES: tuple[str, ...] = (
    "addEventListener",
    "atob",
    "blur",
    "btoa",
    "cancelAnimationFrame",
    "clearInterval",
    "clearTimeout",
    "closed",
    "crypto",
    "devicePixelRatio",
    "dispatchEvent",
    "document",
    "fetch",
    "focus",
    "frames",
    "getComputedStyle",
    "history",
    "innerHeight",
    "innerWidth",
    "length",
    "localStorage",
    "location",
    "locationbar",
    "matchMedia",
    "menubar",
    "name",
    "navigator",
    "onblur",
    "ondevicemotion",
    "onfocus",
    "onpagehide",
    "onpageshow",
    "onpageswap",
    "opener",
    "outerHeight",
    "outerWidth",
    "pageXOffset",
    "pageYOffset",
    "parent",
    "performance",
    "personalbar",
    "queueMicrotask",
    "removeEventListener",
    "requestAnimationFrame",
    "screen",
    "screenLeft",
    "screenTop",
    "screenX",
    "screenY",
    "scrollBy",
    "scrollTo",
    "scrollX",
    "scrollY",
    "scrollbars",
    "self",
    "sessionStorage",
    "setInterval",
    "setTimeout",
    "statusbar",
    "toolbar",
    "top",
    "webkitRequestAnimationFrame",
    "window",
)


def _draw(candidates: Sequence[str], seed: str, domain: str) -> str:
    """Deterministically pick one candidate for ``domain``/``seed``.

    Separate domains on purpose: one shared draw per field would make the three
    probes correlate (account A and B differing in one field would differ in
    all three), which is not what independent page-load sampling looks like.
    """
    digest = hashlib.sha256(f"{domain}/{seed}".encode("utf-8", "surrogatepass")).digest()
    index = int.from_bytes(digest[:8], "big") % len(candidates)
    return str(candidates[index])


def _profile_value(field: str, profile: Mapping[str, Any]) -> str:
    """Render the profile field the same way ``String(navigator[key])`` would.

    ``languages`` is the only one that is not a scalar: the shim stores the
    array the runner passes in, and ``String(array)`` joins with commas and no
    space — the same rendering ``p[8]`` uses.
    """
    if field == "lang_full":
        raw = str(profile.get("lang_full") or profile.get("lang") or "en-US")
        parts = [item.split(";", 1)[0].strip() for item in raw.split(",")]
        return ",".join(part for part in parts if part)
    if field == "max_touch_points":
        try:
            return str(int(str(profile.get("max_touch_points") or 0)))
        except (TypeError, ValueError):
            return "0"
    value = profile.get(field)
    return str(value if value is not None else "")


def navigator_probe(seed: str, profile: Mapping[str, Any]) -> str:
    """``p[10]``: one navigator key plus its rendered value (``"key−value"``)."""
    keys: list[str] = sorted(NAVIGATOR_PROBES) + sorted(NAVIGATOR_PROFILE_PROBES)
    key = _draw(keys, seed, "nav")
    if key in NAVIGATOR_PROFILE_PROBES:
        return f"{key}{SEPARATOR}{_profile_value(NAVIGATOR_PROFILE_PROBES[key], profile)}"
    rendered = NAVIGATOR_PROBES[key]
    # ``undefined`` cannot be stringified by the SDK either: it catches and
    # returns the bare key (see ``NAVIGATOR_PROBES``).
    return key if rendered is None else f"{key}{SEPARATOR}{rendered}"


def document_probe(seed: str) -> str:
    """``p[11]``: one ``Object.keys(document)`` entry."""
    return _draw(DOCUMENT_PROBES, seed, "doc")


def window_probe(seed: str) -> str:
    """``p[12]``: one ``Object.keys(window)`` entry."""
    return _draw(WINDOW_PROBES, seed, "win")


def random_probes(seed: str, profile: Mapping[str, Any]) -> tuple[str, str, str]:
    """The three probes for one account, in ``p[10]``/``p[11]``/``p[12]`` order."""
    return (
        navigator_probe(seed, profile),
        document_probe(seed),
        window_probe(seed),
    )


__all__ = [
    "DOCUMENT_PROBES",
    "NAVIGATOR_PROBES",
    "NAVIGATOR_PROFILE_PROBES",
    "SEPARATOR",
    "WINDOW_PROBES",
    "document_probe",
    "navigator_probe",
    "random_probes",
    "window_probe",
]
