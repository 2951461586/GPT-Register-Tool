"""Low-level DOM field primitives for the PayPal checkout page.

Extracted from ``sms_tool.paypal_auto``. These are generic page-object
helpers (locate / fill / read a field with selector fallbacks) with no
PayPal-specific business meaning, and no dependency on any sibling module.
"""

from __future__ import annotations

import random
import re
import time

def _value_matches(actual: str, expected: str) -> bool:
    actual_s = str(actual or "").strip()
    expected_s = str(expected or "").strip()
    if not expected_s:
        return True
    if actual_s == expected_s:
        return True
    actual_digits = re.sub(r"\D+", "", actual_s)
    expected_digits = re.sub(r"\D+", "", expected_s)
    if expected_digits and actual_digits.endswith(expected_digits):
        return True
    return expected_s.lower() in actual_s.lower()

def _locator_has_value(locator, expected: str) -> bool:
    """Return True when a text-like control visibly kept the expected value."""
    try:
        actual = locator.input_value(timeout=1000)
    except Exception:
        return True
    return _value_matches(actual, expected)

def _set_field_value(locator, value: str, timeout: int = 8000):
    """Set an input value and fire the DOM events PayPal/Stripe listen for."""
    locator.scroll_into_view_if_needed(timeout=timeout)
    locator.click(timeout=timeout)
    try:
        locator.fill(value, timeout=timeout)
    except Exception:
        locator.evaluate(
            """(el, value) => {
                const proto = el instanceof HTMLTextAreaElement
                    ? HTMLTextAreaElement.prototype
                    : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
                setter.call(el, value);
            }""",
            value,
        )
    if value and not _locator_has_value(locator, value):
        try:
            locator.press("Control+A", timeout=timeout)
            locator.type(value, timeout=timeout, delay=random.randint(20, 60))
        except Exception:
            pass
    for event_name in ("input", "change", "blur"):
        try:
            locator.dispatch_event(event_name)
        except Exception:
            pass

def _click_with_fallback(page, selectors: list[str], timeout: int = 8000):
    """Try multiple selectors, click the first one found."""
    for selector in selectors:
        try:
            el = page.locator(selector).first
            if el.is_visible(timeout=3000):
                el.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False

def _fill_with_fallback(page, selectors: list[str], value: str, timeout: int = 8000) -> bool:
    """Try multiple selectors, fill the first one found."""
    for selector in selectors:
        try:
            el = page.locator(selector).first
            if el.is_visible(timeout=3000):
                _set_field_value(el, value, timeout=timeout)
                if not value or _locator_has_value(el, value):
                    return True
        except Exception:
            continue
    return False

def _fill_dom_id(page, element_id: str, value: str) -> bool:
    """Fill PayPal checkoutweb controls by their stable DOM id."""
    scopes = [page, *getattr(page, "frames", [])]
    for scope in scopes:
        try:
            actual = scope.evaluate(
                """({ id, value }) => {
                    const el = document.getElementById(id);
                    if (!el) return null;
                    el.scrollIntoView({ block: "center", inline: "nearest" });
                    const proto =
                        el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype :
                        el instanceof HTMLSelectElement ? HTMLSelectElement.prototype :
                        HTMLInputElement.prototype;
                    const desc = Object.getOwnPropertyDescriptor(proto, "value");
                    if (desc && desc.set) {
                        desc.set.call(el, value);
                    } else {
                        el.value = value;
                    }
                    const events = [
                        new FocusEvent("focus", { bubbles: true }),
                        new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }),
                        new Event("change", { bubbles: true }),
                        new KeyboardEvent("keyup", { bubbles: true }),
                        new FocusEvent("blur", { bubbles: true }),
                        new FocusEvent("focusout", { bubbles: true }),
                    ];
                    for (const event of events) el.dispatchEvent(event);
                    return String(el.value || "");
                }""",
                {"id": element_id, "value": value},
            )
            if actual is not None and _value_matches(actual, value):
                return True
        except Exception:
            continue
    return False

def _fill_dom_ids(page, element_ids: list[str], value: str) -> bool:
    for element_id in element_ids:
        if _fill_dom_id(page, element_id, value):
            return True
    return False

def _field_has_any_value(page, element_ids: list[str]) -> bool:
    for element_id in element_ids:
        for scope in [page, *getattr(page, "frames", [])]:
            try:
                value = scope.evaluate(
                    """(id) => {
                        const el = document.getElementById(id);
                        return el ? String(el.value || "").trim() : null;
                    }""",
                    element_id,
                )
                if value:
                    return True
            except Exception:
                continue
    return False

def _visible_field_has_value(page, selectors: list[str], expected: str = "") -> bool:
    for scope in [page, *getattr(page, "frames", [])]:
        for selector in selectors:
            try:
                el = scope.locator(selector).first
                if not el.is_visible(timeout=1000):
                    continue
                value = el.input_value(timeout=1000)
                if expected:
                    if _value_matches(value, expected):
                        return True
                elif str(value or "").strip():
                    return True
            except Exception:
                continue
    return False

def _fill_by_label_fallback(page, labels: list[str], value: str, timeout: int = 8000) -> bool:
    """Fill PayPal fields whose visible floating label is more stable than CSS attrs."""
    scopes = [page, *getattr(page, "frames", [])]
    for scope in scopes:
        for label in labels:
            for getter in ("get_by_label", "get_by_placeholder"):
                try:
                    el = getattr(scope, getter)(label, exact=False).first
                    if el.is_visible(timeout=1500):
                        _set_field_value(el, value, timeout=timeout)
                        if not value or _locator_has_value(el, value):
                            return True
                except Exception:
                    continue
        try:
            actual = scope.evaluate(
                """({ labels, value }) => {
                    const wanted = labels.map((v) => String(v || "").toLowerCase()).filter(Boolean);
                    const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.visibility !== "hidden" && style.display !== "none" &&
                            rect.width > 0 && rect.height > 0 && !el.disabled && !el.readOnly;
                    };
                    const textAround = (el) => {
                        const parts = [];
                        for (const attr of ["id", "name", "placeholder", "aria-label", "autocomplete", "data-testid"]) {
                            parts.push(el.getAttribute(attr) || "");
                        }
                        let node = el;
                        for (let i = 0; i < 4 && node; i += 1, node = node.parentElement) {
                            parts.push(node.innerText || "");
                        }
                        const id = el.getAttribute("id");
                        if (id) {
                            const label = document.querySelector(`label[for="${CSS.escape(id)}"]`);
                            if (label) parts.push(label.innerText || "");
                        }
                        return parts.join(" ").toLowerCase();
                    };
                    const score = (el) => {
                        const haystack = textAround(el);
                        let best = 0;
                        for (const label of wanted) {
                            if (haystack.includes(label)) best = Math.max(best, 40);
                        }
                        const autocomplete = String(el.getAttribute("autocomplete") || "").toLowerCase();
                        const type = String(el.getAttribute("type") || "").toLowerCase();
                        if (wanted.some((v) => v.includes("email")) && type === "email") best = Math.max(best, 90);
                        if (wanted.some((v) => v.includes("phone")) && type === "tel") best = Math.max(best, 90);
                        if (wanted.some((v) => v.includes("card number")) && autocomplete === "cc-number") best = Math.max(best, 100);
                        if (wanted.some((v) => v.includes("expiration")) && autocomplete === "cc-exp") best = Math.max(best, 100);
                        if (wanted.some((v) => v === "cvv" || v === "cvc") && autocomplete === "cc-csc") best = Math.max(best, 100);
                        if (wanted.some((v) => v.includes("first name")) && autocomplete === "given-name") best = Math.max(best, 100);
                        if (wanted.some((v) => v.includes("last name")) && autocomplete === "family-name") best = Math.max(best, 100);
                        if (wanted.some((v) => v.includes("street")) && autocomplete === "address-line1") best = Math.max(best, 100);
                        if (wanted.some((v) => v.includes("city")) && autocomplete === "address-level2") best = Math.max(best, 100);
                        if (wanted.some((v) => v.includes("zip")) && autocomplete === "postal-code") best = Math.max(best, 100);
                        return best;
                    };
                    const inputs = Array.from(document.querySelectorAll("input, textarea"))
                        .filter(visible)
                        .map((el) => ({ el, score: score(el) }))
                        .filter((item) => item.score > 0)
                        .sort((a, b) => b.score - a.score);
                    if (!inputs.length) return null;
                    const el = inputs[0].el;
                    el.scrollIntoView({ block: "center", inline: "nearest" });
                    const proto = el instanceof HTMLTextAreaElement
                        ? HTMLTextAreaElement.prototype
                        : HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
                    setter.call(el, value);
                    for (const name of ["input", "change", "keyup", "blur", "focusout"]) {
                        el.dispatchEvent(new Event(name, { bubbles: true }));
                    }
                    return String(el.value || "");
                }""",
                {"labels": labels, "value": value},
            )
            if actual is not None and _value_matches(actual, value):
                return True
        except Exception:
            continue
    return False

def _fill_by_visible_label_text(page, label: str, value: str) -> bool:
    """Fill a control by an exact visible floating-label text node."""
    for scope in [page, *getattr(page, "frames", [])]:
        try:
            actual = scope.evaluate(
                """({ label, value }) => {
                    const wanted = String(label || "").trim().toLowerCase();
                    const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.visibility !== "hidden" && style.display !== "none" &&
                            rect.width > 0 && rect.height > 0 && !el.disabled && !el.readOnly;
                    };
                    const setValue = (el) => {
                        el.scrollIntoView({ block: "center", inline: "nearest" });
                        const proto = el instanceof HTMLTextAreaElement
                            ? HTMLTextAreaElement.prototype
                            : HTMLInputElement.prototype;
                        const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
                        if (setter) setter.call(el, value);
                        else el.value = value;
                        for (const name of ["focus", "input", "change", "keyup", "blur", "focusout"]) {
                            el.dispatchEvent(new Event(name, { bubbles: true }));
                        }
                        return String(el.value || "");
                    };
                    const labelEls = Array.from(document.querySelectorAll("label, span, div, p"))
                        .filter((el) => visible(el) && String(el.textContent || "").trim().toLowerCase() === wanted);
                    for (const labelEl of labelEls) {
                        let node = labelEl;
                        for (let depth = 0; depth < 6 && node; depth += 1, node = node.parentElement) {
                            const inputs = Array.from(node.querySelectorAll("input, textarea")).filter(visible);
                            if (inputs.length === 1) return setValue(inputs[0]);
                            if (inputs.length > 1) {
                                const lr = labelEl.getBoundingClientRect();
                                const lx = (lr.left + lr.right) / 2;
                                const ly = (lr.top + lr.bottom) / 2;
                                const containing = inputs
                                    .map((input) => ({ input, rect: input.getBoundingClientRect() }))
                                    .filter(({ rect }) => lx >= rect.left && lx <= rect.right && ly >= rect.top && ly <= rect.bottom)
                                    .sort((a, b) => (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height));
                                if (containing.length) return setValue(containing[0].input);
                                const below = inputs
                                    .map((input) => {
                                        const r = input.getBoundingClientRect();
                                        const dx = Math.abs((r.left + r.right) / 2 - lx);
                                        const dy = Math.max(0, r.top - lr.top);
                                        return { input, distance: dx + dy, rect: r };
                                    })
                                    .filter((item) => item.rect.bottom >= lr.top - 4)
                                    .sort((a, b) => a.distance - b.distance);
                                if (below.length) return setValue(below[0].input);
                                const sorted = inputs
                                    .map((input) => {
                                        const r = input.getBoundingClientRect();
                                        const dx = Math.abs((r.left + r.right) / 2 - lx);
                                        const dy = Math.abs((r.top + r.bottom) / 2 - ly);
                                        return { input, distance: dx + dy };
                                    })
                                    .sort((a, b) => a.distance - b.distance);
                                return setValue(sorted[0].input);
                            }
                        }
                    }
                    return null;
                }""",
                {"label": label, "value": value},
            )
            if actual is not None and _value_matches(actual, value):
                return True
        except Exception:
            continue
    return False

def _fill_visible_input(page, selectors: list[str], value: str, timeout: int = 8000) -> bool:
    """Click and type into a visible input, then verify its value."""
    scopes = [page, *getattr(page, "frames", [])]
    for scope in scopes:
        for selector in selectors:
            try:
                el = scope.locator(selector).first
                if not el.is_visible(timeout=1500):
                    continue
                el.scroll_into_view_if_needed(timeout=timeout)
                el.click(timeout=timeout)
                try:
                    el.press("Control+A", timeout=timeout)
                except Exception:
                    pass
                el.type(value, timeout=timeout, delay=random.randint(25, 70))
                for event_name in ("input", "change", "blur", "focusout"):
                    try:
                        el.dispatch_event(event_name)
                    except Exception:
                        pass
                if _locator_has_value(el, value):
                    return True
            except Exception:
                continue
    return False

def _select_with_fallback(page, selectors: list[str], value: str, labels: list[str] | None = None, timeout: int = 8000) -> bool:
    """Select an option by value/text and dispatch change events."""
    labels = labels or []
    wanted = [value, *labels]
    for selector in selectors:
        try:
            el = page.locator(selector).first
            if not el.is_visible(timeout=3000):
                continue
            for option in wanted:
                try:
                    el.select_option(value=option, timeout=timeout)
                    el.dispatch_event("change")
                    return True
                except Exception:
                    pass
            matched = el.evaluate(
                """(select, wanted) => {
                    const lower = wanted.map((v) => String(v || "").toLowerCase()).filter(Boolean);
                    for (const option of select.options || []) {
                        const value = String(option.value || "").toLowerCase();
                        const text = String(option.textContent || "").toLowerCase();
                        if (lower.some((item) => value === item || text === item || text.includes(item))) {
                            select.value = option.value;
                            select.dispatchEvent(new Event("input", { bubbles: true }));
                            select.dispatchEvent(new Event("change", { bubbles: true }));
                            select.dispatchEvent(new Event("blur", { bubbles: true }));
                            return true;
                        }
                    }
                    return false;
                }""",
                wanted,
            )
            if matched:
                return True
        except Exception:
            continue
    return False

def _read_field_value(page, element_ids: list[str], selectors: list[str]) -> str:
    for element_id in element_ids:
        for scope in [page, *getattr(page, "frames", [])]:
            try:
                value = scope.evaluate(
                    """(id) => {
                        const el = document.getElementById(id);
                        return el ? String(el.value || "").trim() : null;
                    }""",
                    element_id,
                )
                if value:
                    return value
            except Exception:
                continue
    for scope in [page, *getattr(page, "frames", [])]:
        for selector in selectors:
            try:
                el = scope.locator(selector).first
                if not el.is_visible(timeout=1000):
                    continue
                value = el.input_value(timeout=1000).strip()
                if value:
                    return value
            except Exception:
                continue
    return ""

def _type_human(page, selector: str, text: str, delay_range: tuple = (50, 150)):
    """Type text with human-like delays."""
    el = page.locator(selector).first
    el.click()
    for char in text:
        el.type(char, delay=random.randint(*delay_range))
        time.sleep(random.uniform(0.02, 0.08))
