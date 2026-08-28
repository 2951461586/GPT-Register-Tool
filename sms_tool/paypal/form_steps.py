"""PayPal checkout form field filling (business-aware page steps).

Extracted from ``sms_tool.paypal_auto``. Each helper targets one semantic
field on the PayPal signup / checkout form (email, name, card, billing
address, ...) and delegates the mechanical work to ``dom_fields``.
"""

from __future__ import annotations

import time

from .dom_fields import (
    _click_with_fallback,
    _fill_by_label_fallback,
    _fill_by_visible_label_text,
    _fill_dom_ids,
    _fill_visible_input,
    _fill_with_fallback,
    _read_field_value,
    _select_with_fallback,
    _visible_field_has_value,
)
from .errors import _PayPalStepError
from .session import _dismiss_overlays, _wait_for_checkout_form_after_email

def _ensure_country_us(page):
    """Set the country/region selector to United States when present."""
    selectors = [
        'select[id="country"]',
        'select[name="country"]',
        'select[autocomplete="country"]',
        'select[aria-label*="Country"]',
        'select[aria-label*="region"]',
        '#country',
    ]
    for selector in selectors:
        try:
            el = page.locator(selector).first
            if not el.is_visible(timeout=1000):
                continue
            current = el.evaluate(
                """(select) => {
                    const value = String(select.value || "").toLowerCase();
                    const text = String(select.options?.[select.selectedIndex]?.textContent || "").toLowerCase();
                    return { value, text };
                }"""
            )
            if current and (current.get("value") in ("us", "usa", "united states") or "united states" in current.get("text", "")):
                return False
        except Exception:
            continue
    if _select_with_fallback(page, selectors, "US", labels=["United States", "United States of America"], timeout=5000):
        print("[*] Country/region set to US")
        time.sleep(2)
        return True
    return False

def _select_openai_checkout_paypal(page) -> bool:
    selectors = [
        '[data-testid="paypal-accordion-item"]',
        '#payment-method-accordion-item-title-paypal',
        'button[data-testid="paypal-accordion-item-button"]',
        'button[aria-label*="PayPal" i]',
        'label:has-text("PayPal")',
        '[role="radio"]:has-text("PayPal")',
        '[role="button"]:has-text("PayPal")',
        'text="PayPal"',
    ]
    return _click_with_fallback(page, selectors, timeout=5000)

def _click_openai_checkout_continue(page) -> bool:
    selectors = [
        'button:has-text("Continue")',
        'button:has-text("Subscribe")',
        'button:has-text("Start free trial")',
        'button:has-text("Start trial")',
        'button:has-text("Pay")',
        'button[type="submit"]',
        '[data-testid="hosted-payment-submit-button"]',
    ]
    return _click_with_fallback(page, selectors, timeout=8000)

def _fill_openai_checkout_billing(page, address: dict, first_name: str, last_name: str, phone: str) -> None:
    full_name = f"{first_name} {last_name}".strip()
    country = str(address.get("country") or "US")
    line1 = str(address.get("line1") or "")
    city = str(address.get("city") or "")
    state = str(address.get("state") or "")
    postal_code = str(address.get("postal_code") or address.get("zip") or "")
    fields = [
        (["#billingName", 'input[name="billingName"]', 'input[autocomplete="billing name"]'], full_name),
        (["#billingAddressLine1", 'input[name="billingAddressLine1"]', 'input[autocomplete="billing address-line1"]'], line1),
        (["#billingLocality", 'input[name="billingLocality"]', 'input[autocomplete="billing address-level2"]'], city),
        (["#billingAdministrativeArea", 'input[name="billingAdministrativeArea"]', 'input[autocomplete="billing address-level1"]'], state),
        (["#billingPostalCode", 'input[name="billingPostalCode"]', 'input[autocomplete="billing postal-code"]'], postal_code),
        (["#phoneNumber", 'input[name="phoneNumber"]', 'input[type="tel"]'], phone),
    ]
    for selectors, value in fields:
        if value:
            _fill_visible_input(page, selectors, value, timeout=1500) or _fill_with_fallback(page, selectors, value, timeout=1500)
    if country:
        _select_with_fallback(
            page,
            ["#billingCountry", 'select[name="billingCountry"]', 'select[autocomplete="billing country"]'],
            country,
            labels=["United States", "United States of America"],
            timeout=1500,
        )

def _click_create_account(page):
    """Click 'Create an account' link on PayPal login page."""
    selectors = [
        'text="Create an account"',
        'text="Sign Up"',
        'a:has-text("Create")',
        'a:has-text("Sign up")',
        '[data-testid="signup-link"]',
        'a[href*="signup"]',
        'button:has-text("Create")',
    ]
    card_selectors = [
        'text="Pay with Debit or Credit Card"',
        'text="Pay by Debit or Credit Card"',
        'button:has-text("Debit or Credit")',
        '[data-testid="guest-checkout-button"]',
    ]
    if _click_with_fallback(page, card_selectors, timeout=5000):
        print("[*] Clicked 'Pay with Debit or Credit Card'")
        time.sleep(2)
        return
    if _click_with_fallback(page, selectors, timeout=8000):
        print("[*] Clicked 'Create an account'")
        time.sleep(2)
        return
    print("[*] No create-account button found, assuming already on form")

def _fill_signup_email(page, email: str):
    """Fill email on PayPal signup or guest checkout form."""
    _ensure_country_us(page)
    selectors = [
        'input[id="email"]',
        '#email',
        'input[name="email"]',
        'input[id*="email" i]',
        'input[name*="email" i]',
        'input[type="email"]',
        'input[autocomplete="email"]',
        'input[aria-label*="email" i]',
        'input[placeholder*="email" i]',
        'input[data-testid="email-input"]',
        '[data-testid*="email" i] input',
    ]
    if _fill_dom_ids(page, ["email"], email) or _fill_visible_input(page, selectors, email) or _fill_by_label_fallback(page, ["Email", "Email address"], email) or _fill_with_fallback(page, selectors, email):
        print(f"[*] Email filled: {email}")
        time.sleep(1)
        if not _visible_field_has_value(page, selectors, email):
            raise _PayPalStepError("fill_email", "email field stayed blank after fill")
        # PayPal checkoutweb keeps all fields on one form; avoid the final submit button here.
        if _click_with_fallback(page, [
            'button:has-text("Next")',
            'button:has-text("Continue")',
        ], timeout=3000):
            time.sleep(2)
            if not _wait_for_checkout_form_after_email(page):
                raise _PayPalStepError("fill_email", "checkout form did not appear after email continue")
            if not _visible_field_has_value(page, selectors, email):
                _fill_visible_input(page, selectors, email, timeout=5000)
        return
    raise _PayPalStepError("fill_email", "email field not found")

def _fill_signup_name(page, first_name: str, last_name: str):
    """Fill name fields on PayPal signup."""
    first_selectors = [
        'input[id="first-name"]', 'input[name="firstName"]',
        'input[name="first_name"]', 'input[autocomplete="given-name"]',
        'input[placeholder*="First name" i]', 'input[aria-label*="First name" i]',
        '#firstName', '#first-name',
    ]
    first_ok = _fill_dom_ids(page, ["firstName", "first-name"], first_name) or _fill_by_visible_label_text(page, "First name", first_name) or _fill_visible_input(page, first_selectors, first_name) or _fill_by_label_fallback(page, ["First name", "Given name"], first_name) or _fill_with_fallback(page, first_selectors, first_name)
    time.sleep(random.uniform(0.3, 0.8))

    last_selectors = [
        'input[id="last-name"]', 'input[name="lastName"]',
        'input[name="last_name"]', 'input[autocomplete="family-name"]',
        'input[placeholder*="Last name" i]', 'input[aria-label*="Last name" i]',
        '#lastName', '#last-name',
    ]
    last_ok = _fill_dom_ids(page, ["lastName", "last-name"], last_name) or _fill_by_visible_label_text(page, "Last name", last_name) or _fill_visible_input(page, last_selectors, last_name) or _fill_by_label_fallback(page, ["Last name", "Family name", "Surname"], last_name) or _fill_with_fallback(page, last_selectors, last_name)
    time.sleep(random.uniform(0.3, 0.8))
    if not first_ok:
        print("[!] First name field not filled")
    if not last_ok:
        print("[!] Last name field not filled")
    if first_ok and last_ok:
        print(f"[*] Name filled: {first_name} {last_name}")

def _fill_phone_if_present(page, phone: str):
    """Fill phone number if the field is visible."""
    selectors = [
        'input[id="phone"]', 'input[name="phone"]',
        'input[type="tel"]', 'input[autocomplete="tel"]',
        '#phoneNumber', '#phone',
    ]
    phone_value = re.sub(r"\D+", "", phone or "")
    if phone_value.startswith("1") and len(phone_value) > 10:
        phone_value = phone_value[1:]
    if _fill_dom_ids(page, ["phone", "phoneNumber"], phone_value) or _fill_visible_input(page, selectors, phone_value, timeout=3000) or _fill_by_label_fallback(page, ["Phone number", "Mobile number", "Phone"], phone_value, timeout=3000) or _fill_with_fallback(page, selectors, phone_value, timeout=3000):
        print(f"[*] Phone filled: {phone_value}")
        time.sleep(1)
        _click_with_fallback(page, [
            'button:has-text("Send Code")',
            'button:has-text("Send")',
            'button:has-text("Get Code")',
        ], timeout=3000)
        time.sleep(2)

def _fill_password(page, password: str):
    """Fill password fields on PayPal signup."""
    selectors = [
        'input[id="password"]', 'input[name="password"]',
        'input[type="password"]', '#createPassword', '#password',
    ]
    if _fill_dom_ids(page, ["password", "createPassword"], password) or _fill_visible_input(page, selectors, password) or _fill_by_label_fallback(page, ["Create password", "Password"], password) or _fill_with_fallback(page, selectors, password):
        print(f"[*] Password filled")
        time.sleep(random.uniform(0.3, 0.8))
        confirm_selectors = [
            'input[id="confirm-password"]', 'input[name="confirmPassword"]',
            '#confirmPassword', '#confirm-password',
        ]
        _fill_with_fallback(page, confirm_selectors, password, timeout=3000)
        time.sleep(1)

def _fill_card(page, card: dict):
    """Fill card number, expiry, CVV."""
    number = card.get("number", "")
    exp_month = card.get("exp_month", "")
    exp_year = card.get("exp_year", "")
    cvv = card.get("cvv", "")

    card_selectors = [
        'input[name="cardNumber"]', 'input[id="cardNumber"]',
        'input[autocomplete="cc-number"]', 'input[name="card_number"]',
        'input[placeholder*="Card"]', 'input[placeholder*="card"]',
        '#card-number', 'input[data-testid="card-number-input"]',
    ]
    if not (_fill_dom_ids(page, ["cardNumber"], number) or _fill_visible_input(page, card_selectors, number, timeout=10000) or _fill_by_label_fallback(page, ["Card number", "Credit or debit card number"], number, timeout=10000) or _fill_with_fallback(page, card_selectors, number, timeout=10000)):
        try:
            for frame in page.frames:
                for sel in card_selectors:
                    try:
                        el = frame.locator(sel).first
                        if el.is_visible(timeout=2000):
                            el.click()
                            el.fill(number)
                            print(f"[*] Card number filled (iframe)")
                            break
                    except Exception:
                        continue
        except Exception:
            pass
    else:
        print("[*] Card number filled: [REDACTED]")
    time.sleep(random.uniform(0.5, 1.0))

    month_selectors = [
        'select[name*="month"]', 'select[id*="month"]',
        'select[autocomplete="cc-exp-month"]', '#expiration-month',
    ]
    if not _fill_with_fallback(page, month_selectors, "", timeout=3000):
        exp_selectors = [
            'input[name="expirationDate"]', 'input[name*="exp"]',
            'input[autocomplete="cc-exp"]', 'input[placeholder*="MM"]',
            '#expiration-date', '#expiry',
        ]
        exp_str = f"{exp_month}/{exp_year[-2:]}"
        _fill_dom_ids(page, ["cardExpiry"], exp_str) or _fill_visible_input(page, exp_selectors, exp_str, timeout=5000) or _fill_by_label_fallback(page, ["Expiration date", "Expiry date", "MM/YY"], exp_str, timeout=5000) or _fill_with_fallback(page, exp_selectors, exp_str, timeout=5000)
    else:
        try:
            page.locator(month_selectors[0]).first.select_option(value=exp_month)
        except Exception:
            pass
    time.sleep(random.uniform(0.3, 0.8))

    year_selectors = [
        'select[name*="year"]', 'select[id*="year"]',
        'select[autocomplete="cc-exp-year"]', '#expiration-year',
    ]
    try:
        page.locator(year_selectors[0]).first.select_option(value=exp_year)
    except Exception:
        pass
    time.sleep(random.uniform(0.3, 0.8))

    cvv_selectors = [
        'input[name="cvv"]', 'input[name="cvc"]', 'input[name="cvvNumber"]',
        'input[autocomplete="cc-csc"]', 'input[placeholder*="CVV"]',
        'input[placeholder*="CVC"]', '#cvv', '#cvc',
        'input[data-testid="cvv-input"]',
    ]
    _fill_dom_ids(page, ["cardCvv", "cvv", "cvc"], cvv) or _fill_visible_input(page, cvv_selectors, cvv, timeout=5000) or _fill_by_label_fallback(page, ["CVV", "CVC", "Security code"], cvv, timeout=5000) or _fill_with_fallback(page, cvv_selectors, cvv, timeout=5000)
    print(f"[*] CVV filled")
    time.sleep(random.uniform(0.5, 1.0))

def _fill_billing_address(page, address: dict):
    """Fill billing address fields."""
    line1 = address.get("line1", "")
    city = address.get("city", "")
    state = address.get("state", "")
    postal_code = address.get("postal_code", "")
    first_name = address.get("first_name", "")
    last_name = address.get("last_name", "")

    _ensure_country_us(page)

    if first_name or last_name:
        _fill_signup_name(page, first_name, last_name)

    addr_selectors = [
        '#billingLine1', '#billingAddressLine1',
        'input[name="billingLine1"]', 'input[name="billingAddressLine1"]',
        'input[name="line1"]', 'input[name="addressLine1"]',
        'input[name*="billing" i][name*="line1" i]',
        'input[id*="billing" i][id*="line1" i]',
        'input[placeholder*="Street address" i]', 'input[aria-label*="Street address" i]',
        'input[name="streetAddress"]', 'input[autocomplete="address-line1"]',
        '#addressLine1', '#street-address', '#line1',
    ]
    if not (_fill_dom_ids(page, ["billingLine1", "billingAddressLine1", "addressLine1"], line1) or _fill_by_visible_label_text(page, "Street address", line1) or _fill_visible_input(page, addr_selectors, line1, timeout=5000) or _fill_by_label_fallback(page, ["Street address", "Address line 1"], line1, timeout=5000) or _fill_with_fallback(page, addr_selectors, line1, timeout=5000)):
        print("[!] Address line1 field not found")
    _dismiss_overlays(page)
    time.sleep(random.uniform(0.3, 0.8))

    city_selectors = [
        '#billingCity', '#billingLocality',
        'input[name="billingCity"]', 'input[name="billingLocality"]',
        'input[name="city"]', 'input[name="addressCity"]',
        'input[name*="billing" i][name*="city" i]',
        'input[id*="billing" i][id*="city" i]',
        'input[id*="locality" i]', 'input[name*="locality" i]',
        'input[autocomplete="address-level2"]', '#city', '#addressCity',
    ]
    if not (_fill_dom_ids(page, ["billingCity", "billingLocality", "city"], city) or _fill_by_visible_label_text(page, "City", city) or _fill_visible_input(page, city_selectors, city, timeout=5000) or _fill_by_label_fallback(page, ["City", "Town"], city, timeout=5000) or _fill_with_fallback(page, city_selectors, city, timeout=5000)):
        print("[!] Billing city field not found")
    _dismiss_overlays(page)
    time.sleep(random.uniform(0.3, 0.8))

    state_selectors = [
        '#billingState', '#billingAdministrativeArea',
        'select[name="billingState"]', 'select[name="billingAdministrativeArea"]',
        'select[name="state"]', 'select[name="addressState"]',
        'select[name*="billing" i][name*="state" i]',
        'select[name*="administrativeArea" i]',
        'select[id*="billing" i][id*="state" i]',
        'select[id*="AdministrativeArea" i]',
        'select[id*="state"]', '#state',
    ]
    if not _select_with_fallback(page, state_selectors, state, timeout=5000):
        state_text_selectors = [
            '#billingState', '#billingAdministrativeArea',
            'input[name="billingState"]', 'input[name="billingAdministrativeArea"]',
            'input[name="state"]', 'input[name="addressState"]',
            'input[name*="billing" i][name*="state" i]',
            'input[name*="administrativeArea" i]',
            'input[id*="billing" i][id*="state" i]',
            '#state-input',
        ]
        if not (_fill_dom_ids(page, ["billingState", "billingAdministrativeArea", "state"], state) or _fill_visible_input(page, state_text_selectors, state, timeout=3000) or _fill_by_label_fallback(page, ["State", "Province"], state, timeout=3000) or _fill_with_fallback(page, state_text_selectors, state, timeout=3000)):
            print("[!] Billing state field not found")
    time.sleep(random.uniform(0.3, 0.8))

    zip_selectors = [
        '#billingPostalCode',
        'input[name="billingPostalCode"]',
        'input[name="postalCode"]', 'input[name="zip"]',
        'input[name*="billing" i][name*="postal" i]',
        'input[id*="billing" i][id*="postal" i]',
        'input[autocomplete="postal-code"]', '#postalCode', '#zip',
    ]
    if not (_fill_dom_ids(page, ["billingPostalCode", "postalCode", "zip"], postal_code) or _fill_by_visible_label_text(page, "ZIP code", postal_code) or _fill_visible_input(page, zip_selectors, postal_code, timeout=5000) or _fill_by_label_fallback(page, ["ZIP code", "Postal code", "Zip"], postal_code, timeout=5000) or _fill_with_fallback(page, zip_selectors, postal_code, timeout=5000)):
        print("[!] Billing postal code field not found")
    _dismiss_overlays(page)
    print(f"[*] Address filled: {line1}, {city}, {state} {postal_code}")
    time.sleep(random.uniform(0.5, 1.0))

def _verify_checkout_fields(page):
    """Fail fast when PayPal's checkoutweb form still has blank required fields."""
    fields = {
        "email": (["email"], ['input[id="email"]', '#email', 'input[name="email"]', 'input[type="email"]', 'input[placeholder*="email" i]']),
        "phone": (["phone", "phoneNumber"], ['input[id="phone"]', 'input[name="phone"]', 'input[type="tel"]', 'input[placeholder*="phone" i]']),
        "cardNumber": (["cardNumber"], ['input[id="cardNumber"]', '#cardNumber', 'input[name="cardNumber"]', 'input[autocomplete="cc-number"]', 'input[placeholder*="Card" i]']),
        "cardExpiry": (["cardExpiry"], ['input[id="cardExpiry"]', '#cardExpiry', 'input[name="cardExpiry"]', 'input[autocomplete="cc-exp"]', 'input[placeholder*="Expiration" i]', 'input[placeholder*="MM" i]']),
        "cardCvv": (["cardCvv", "cvv", "cvc"], ['input[id="cardCvv"]', '#cardCvv', 'input[name="cardCvv"]', 'input[name="cvv"]', 'input[name="cvc"]', 'input[autocomplete="cc-csc"]', 'input[placeholder*="CVV" i]']),
        "firstName": (["firstName", "first-name"], ['input[id="firstName"]', '#firstName', 'input[name="firstName"]', 'input[autocomplete="given-name"]', 'input[placeholder*="First name" i]']),
        "lastName": (["lastName", "last-name"], ['input[id="lastName"]', '#lastName', 'input[name="lastName"]', 'input[autocomplete="family-name"]', 'input[placeholder*="Last name" i]']),
        "billingLine1": (["billingLine1", "billingAddressLine1", "addressLine1"], ['input[id="billingLine1"]', '#billingLine1', 'input[name="billingLine1"]', 'input[autocomplete="address-line1"]', 'input[placeholder*="Street address" i]']),
        "billingCity": (["billingCity", "billingLocality", "city"], ['input[id="billingCity"]', '#billingCity', 'input[name="billingCity"]', 'input[autocomplete="address-level2"]', 'input[placeholder*="City" i]']),
        "billingPostalCode": (["billingPostalCode", "postalCode", "zip"], ['input[id="billingPostalCode"]', '#billingPostalCode', 'input[name="billingPostalCode"]', 'input[autocomplete="postal-code"]', 'input[placeholder*="ZIP" i]']),
        "password": (["password", "createPassword"], ['input[id="password"]', '#password', 'input[name="password"]', 'input[type="password"]', 'input[placeholder*="password" i]']),
    }
    values = {name: _read_field_value(page, ids, selectors) for name, (ids, selectors) in fields.items()}
    required = ["email", "cardNumber", "cardExpiry", "cardCvv", "firstName", "lastName", "billingLine1", "billingCity", "billingPostalCode", "password"]
    missing = [element_id for element_id in required if not values.get(element_id)]
    if missing:
        raise _PayPalStepError("verify_fields", f"blank PayPal field(s): {', '.join(missing)}")
    masked = dict(values)
    if masked.get("cardNumber"):
        masked["cardNumber"] = "[REDACTED]"
    if masked.get("cardCvv"):
        masked["cardCvv"] = "[REDACTED]"
    if masked.get("password"):
        masked["password"] = "[REDACTED]"
    print(f"[*] PayPal fields verified: {masked}")

def _accept_terms(page):
    """Check terms checkbox and click agree."""
    checkbox_selectors = [
        'input[type="checkbox"][name*="agree"]',
        'input[type="checkbox"][name*="terms"]',
        'input[type="checkbox"][id*="agree"]',
        'input[type="checkbox"][id*="terms"]',
        '[data-testid="agreement-checkbox"]',
    ]
    for selector in checkbox_selectors:
        try:
            el = page.locator(selector).first
            if el.is_visible(timeout=2000) and not el.is_checked():
                el.check()
                print("[*] Terms checkbox checked")
                break
        except Exception:
            continue
    time.sleep(1)
