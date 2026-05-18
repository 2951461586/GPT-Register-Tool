"""PayPal auto-payment via reverse-engineered HTTP protocol.

Primary flow for PayPal authorization: parses forms, submits via HTTP,
and follows redirect chain to capture OAuth tokens. Falls back to browser
when encountering CAPTCHA or JS-only rendering.

Uses curl_cffi for Chrome TLS fingerprint impersonation.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

try:
    from curl_cffi.requests import Session as CurlSession
except ImportError:
    CurlSession = None

import requests as _requests

from .config import CFG

# Re-exported from paypal_auto (shared with browser flow)
from .paypal_auto import _extract_sms_code, _sms_baseline


# ──────────────────────────── data types ────────────────────────────


@dataclass
class ReversePayResult:
    ok: bool
    email: str = ""
    error: str = ""
    failed_step: str = ""
    access_token: str = ""
    oauth_refresh_token: str = ""
    refresh_token_status: str = ""
    paypal_status: str = ""
    redirect_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"ok": self.ok, "email": self.email}
        if self.ok:
            d.update({
                "access_token": self.access_token,
                "oauth_refresh_token": self.oauth_refresh_token,
                "refresh_token_status": self.refresh_token_status,
                "paypal_status": self.paypal_status,
                "redirect_url": self.redirect_url,
            })
        else:
            d.update({"error": self.error, "failed_step": self.failed_step})
        return d


# ──────────────────────────── constants ────────────────────────────

_CHROME_VERSION = "136"
_CHROME_FULL_VERSION = "136.0.7103.93"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{_CHROME_VERSION}.0.0.0 Safari/537.36"
)

# Patterns indicating JS-only rendering or CAPTCHA
_CAPTCHA_PATTERNS = [
    re.compile(r"data-app=[\"']?authchallenge_response", re.I),
    re.compile(r"id=[\"']?captcha-standalone", re.I),
    re.compile(r"data-enable-ads-captcha=[\"']?true", re.I),
    re.compile(r"adsddcaptcha", re.I),
    re.compile(r"ngrlCaptcha", re.I),
    re.compile(r"g-recaptcha", re.I),
    re.compile(r"recaptcha", re.I),
    re.compile(r"are you a human", re.I),
    re.compile(r"verify you are human", re.I),
]

_BLOCK_PATTERNS = [
    re.compile(r"unusual activity", re.I),
    re.compile(r"temporarily locked", re.I),
    re.compile(r"try again later", re.I),
    re.compile(r"access denied", re.I),
    re.compile(r"unable to process", re.I),
]


# ──────────────────────────── client ────────────────────────────


class PayPalReverseClient:
    """Reverse-engineered PayPal authorization client.

    Attempts to complete the PayPal signup + payment authorization
    via HTTP requests instead of browser automation.
    """

    def __init__(
        self,
        redirect_url: str,
        card: dict,
        address: dict,
        first_name: str,
        last_name: str,
        alias_email: str,
        password: str,
        phone: str,
        sms_cfg: dict,
        proxy: str | None = None,
        cookie_header: str = "",
        timeout: int = 60,
    ):
        self.redirect_url = redirect_url
        self.card = card
        self.address = address
        self.first_name = first_name
        self.last_name = last_name
        self.alias_email = alias_email
        self.password = password
        self.phone = phone
        self.sms_cfg = sms_cfg
        self.proxy = proxy
        self.cookie_header = cookie_header
        self.timeout = timeout

        self._session: Any = None
        self._current_url: str = ""
        self._current_html: str = ""
        self._csrf_token: str = ""
        self._captcha_token: str = ""
        self._captcha_ekey: str = ""
        self._nodriver_cookies: dict[str, str] = {}
        self._captcha_solved_by_nodriver: bool = False

    # ──────────────── public entry ────────────────

    def authorize(self) -> ReversePayResult:
        """Execute the full PayPal authorization flow via HTTP."""
        try:
            self._session = self._new_session()
            return self._do_authorize()
        except _NeedBrowserFallback as e:
            return ReversePayResult(ok=False, error=str(e), failed_step=e.step)
        except Exception as e:
            return ReversePayResult(ok=False, error=str(e), failed_step="unknown")
        finally:
            if self._session:
                try:
                    self._session.close()
                except Exception:
                    pass

    def _do_authorize(self) -> ReversePayResult:
        # 1. Parse redirect URL and load initial page
        self._parse_redirect_url()
        self._load_cookies()
        self._load_initial_page()

        # 2. Create PayPal account
        self._create_account()

        # 3. Handle SMS verification
        code = self._handle_sms()
        if code:
            print(f"[re] SMS code: {code}")

        # 4. Fill card and billing
        self._fill_card_and_billing()

        # 5. Submit payment
        self._submit_payment()

        # 6. Extract auth tokens
        result = self._extract_auth_tokens()
        if result.ok:
            result.email = self.alias_email
            result.paypal_status = "completed"
        return result

    # ──────────────── session setup ────────────────

    def _new_session(self) -> Any:
        if CurlSession is not None:
            s = CurlSession(impersonate=f"chrome{_CHROME_VERSION}")
        else:
            s = _requests.Session()
        if self.proxy:
            s.proxies = {"http": self.proxy, "https": self.proxy}
        s.headers.update({
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "max-age=0",
            "Priority": "u=0, i",
            "sec-ch-ua": f'"Chromium";v="{_CHROME_VERSION}", "Google Chrome";v="{_CHROME_VERSION}", "Not.A/Brand";v="99"',
            "sec-ch-ua-full-version-list": f'"Chromium";v="{_CHROME_FULL_VERSION}", "Google Chrome";v="{_CHROME_FULL_VERSION}", "Not.A/Brand";v="99.0.0.0"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        })
        return s

    def _parse_redirect_url(self):
        """Extract token parameters from the Stripe → PayPal redirect URL.

        Handles two URL formats:
        1. Direct PayPal: https://www.paypal.com/cgi-bin/webscr?cmd=_express-checkout&token=EC-xxx&ba_token=BA-xxx
        2. Stripe redirect: https://pm-redirects.stripe.com/authorize/acct_xxx/pa_nonce_xxx
           (needs to follow redirect first to get the actual PayPal URL)
        """
        parsed = urlparse(self.redirect_url)

        # Case 1: Already a PayPal URL with token params
        params = parse_qs(parsed.query)
        self._pp_token = (params.get("token") or [""])[0]
        self._ba_token = (params.get("ba_token") or [""])[0]
        self._cmd = (params.get("cmd") or [""])[0]
        if self._pp_token or self._ba_token:
            return

        # Case 2: Stripe redirect URL — follow it to get the real PayPal URL
        if "stripe.com" in parsed.netloc:
            print("[re] Following Stripe redirect to PayPal...")
            r = self._safe_request("GET", self.redirect_url, allow_redirects=False)
            location = r.headers.get("Location") or r.headers.get("location", "")
            if location:
                self.redirect_url = location
                parsed = urlparse(location)
                params = parse_qs(parsed.query)
                self._pp_token = (params.get("token") or [""])[0]
                self._ba_token = (params.get("ba_token") or [""])[0]
                self._cmd = (params.get("cmd") or [""])[0]
                if self._pp_token or self._ba_token:
                    print(f"[re] Got PayPal token: {self._pp_token[:20]}..." if self._pp_token else f"[re] Got BA token: {self._ba_token[:20]}...")
                    return
                # Maybe it redirected again
                if "paypal.com" in parsed.netloc:
                    print(f"[re] Redirected to PayPal: {location[:80]}")
                    return

        # Case 3: Try following a chain of redirects
        if "stripe.com" in parsed.netloc or "paypal.com" not in parsed.netloc:
            r = self._follow_redirects(self.redirect_url, max_hops=5)
            final_url = str(r.url)
            parsed = urlparse(final_url)
            params = parse_qs(parsed.query)
            self._pp_token = (params.get("token") or [""])[0]
            self._ba_token = (params.get("ba_token") or [""])[0]
            self._cmd = (params.get("cmd") or [""])[0]
            self.redirect_url = final_url
            if self._pp_token or self._ba_token:
                return
            # If we landed on a PayPal page, that's fine even without explicit tokens
            if "paypal.com" in parsed.netloc:
                print(f"[re] Landed on PayPal: {final_url[:80]}")
                return

        raise _NeedBrowserFallback("parse_url", "could not resolve PayPal URL from redirect chain")

    def _load_cookies(self):
        """Import existing cookies from session data."""
        if not self.cookie_header:
            return
        for item in str(self.cookie_header).split(";"):
            if "=" not in item:
                continue
            name, value = item.split("=", 1)
            name, value = name.strip(), value.strip()
            if name and value and not name.startswith("__Host-"):
                self._session.cookies.set(name, value, domain=".paypal.com")

    def _load_initial_page(self):
        """Load the PayPal authorization page."""
        r = self._safe_request("GET", self.redirect_url)
        self._current_url = str(r.url)
        self._current_html = r.text
        self._update_csrf(r)

        # Check for immediate blocks
        self._check_blocked(self._current_html, "load_page")

        # If nodriver solved CAPTCHA, reload with updated cookies/URL
        if self._captcha_solved_by_nodriver:
            r = self._safe_request("GET", self._current_url)
            self._current_html = r.text
            self._current_url = str(r.url)
            self._update_csrf(r)
            print(f"[re] Reloaded page after nodriver CAPTCHA bypass: {self._current_url[:80]}")

        # If PayPal redirected to login/signup page, follow
        if "login" in self._current_url.lower() or "signup" in self._current_url.lower():
            print(f"[*] Redirected to: {self._current_url[:80]}")

    # ──────────────── account creation ────────────────

    def _create_account(self):
        """Attempt multi-step PayPal account creation via form submissions."""
        for step_name, handler in [
            ("email", self._step_email),
            ("password", self._step_password),
            ("personal", self._step_personal),
            ("phone", self._step_phone),
        ]:
            try:
                handler()
            except _NeedBrowserFallback:
                raise
            except Exception as e:
                print(f"[re] Step {step_name} failed: {e}")
                raise _NeedBrowserFallback(step_name, str(e))

    def _step_email(self):
        """Submit email on the signup form."""
        form = self._find_signup_form()
        if not form:
            # Maybe already past email step, or page is JS-only
            if self._is_js_only_page():
                raise _NeedBrowserFallback("email", "page requires JavaScript rendering")
            return

        action, fields = form
        fields["email"] = self.alias_email
        # PayPal may use different field names
        for key in ("login_email", "email", "signup_email", "login_emailcopy", "emailAddress", "payerEmail"):
            if key in fields or key == "email":
                fields[key] = self.alias_email
        for key in ("country", "countryCode", "billingCountry", "billingCountryCode"):
            if key in fields:
                fields[key] = "US"

        r = self._submit_form(action, fields)
        self._current_url = str(r.url)
        self._current_html = r.text
        self._update_csrf(r)
        self._check_blocked(self._current_html, "email")

    def _step_password(self):
        """Submit password on the signup form."""
        form = self._find_form_by_fields(["password", "createPassword", "login_password"])
        if not form:
            return

        action, fields = form
        pwd_fields = ["password", "createPassword", "login_password", "newPassword"]
        for key in pwd_fields:
            if key in fields:
                fields[key] = self.password

        r = self._submit_form(action, fields)
        self._current_url = str(r.url)
        self._current_html = r.text
        self._update_csrf(r)
        self._check_blocked(self._current_html, "password")

    def _step_personal(self):
        """Submit personal info (name, etc.)."""
        form = self._find_form_by_fields(["firstName", "first_name", "givenName"])
        if not form:
            return

        action, fields = form
        name_map = {
            "firstName": self.first_name, "first_name": self.first_name,
            "givenName": self.first_name, "given-name": self.first_name,
            "lastName": self.last_name, "last_name": self.last_name,
            "familyName": self.last_name, "family-name": self.last_name,
        }
        for key, value in name_map.items():
            if key in fields:
                fields[key] = value

        r = self._submit_form(action, fields)
        self._current_url = str(r.url)
        self._current_html = r.text
        self._update_csrf(r)
        self._check_blocked(self._current_html, "personal")

    def _step_phone(self):
        """Submit phone number if requested."""
        form = self._find_form_by_fields(["phone", "phoneNumber", "phone_number"])
        if not form:
            return

        action, fields = form
        phone_fields = ["phone", "phoneNumber", "phone_number", "mobilePhone"]
        for key in phone_fields:
            if key in fields:
                fields[key] = self.phone

        r = self._submit_form(action, fields)
        self._current_url = str(r.url)
        self._current_html = r.text
        self._update_csrf(r)
        self._check_blocked(self._current_html, "phone")

    # ──────────────── SMS verification ────────────────

    def _handle_sms(self) -> str | None:
        """Handle SMS verification if the page requires it."""
        if not self._sms_input_present():
            return None

        print("[re] SMS verification required, polling for code...")
        baseline = _sms_baseline(self.sms_cfg.get("api_url", ""))

        # Poll for code
        code = self._poll_sms(
            self.sms_cfg.get("api_url", ""),
            baseline,
            timeout=int(self.sms_cfg.get("timeout", 120)),
            interval=int(self.sms_cfg.get("poll_interval", 5)),
        )
        if not code:
            raise _NeedBrowserFallback("sms", "SMS code timeout")

        # Submit code
        form = self._find_form_by_fields(["code", "smsCode", "otpCode", "verificationCode"])
        if form:
            action, fields = form
            code_fields = ["code", "smsCode", "otpCode", "verificationCode", "otp"]
            for key in code_fields:
                if key in fields:
                    fields[key] = code
            r = self._submit_form(action, fields)
            self._current_url = str(r.url)
            self._current_html = r.text
            self._update_csrf(r)

        return code

    def _sms_input_present(self) -> bool:
        """Check if the current page has an SMS code input."""
        html_lower = self._current_html.lower()
        for pattern in ["smscode", "otpcode", "verificationcode", "input.*code"]:
            if re.search(pattern, html_lower):
                return True
        return False

    def _poll_sms(
        self, api_url: str, baseline: dict, timeout: int = 120, interval: int = 5
    ) -> str | None:
        """Poll SMS API for verification code."""
        if not api_url:
            return None
        deadline = time.time() + timeout
        baseline_raw = baseline.get("raw", "")
        attempt = 0

        while time.time() < deadline:
            attempt += 1
            try:
                r = _requests.get(api_url, timeout=10)
                if r.status_code == 200:
                    text = r.text.strip()
                    if text and text != baseline_raw:
                        code = _extract_sms_code(text)
                        if code:
                            return code
                    if text:
                        code = _extract_sms_code(text)
                        if code and attempt > 2:
                            return code
            except Exception:
                pass
            time.sleep(interval)
        return None

    # ──────────────── card and billing ────────────────

    def _fill_card_and_billing(self):
        """Fill card details and billing address."""
        form = self._find_form_by_fields([
            "cardNumber", "card_number", "cc-number",
            "expirationDate", "expiration_date", "expMonth",
            "billingLine1", "billingAddressLine1", "billingPostalCode",
        ])
        if not form:
            print("[re] No card form found, may already be filled")
            return

        action, fields = form
        card = self.card
        addr = self.address

        # Card fields
        card_map = {
            "cardNumber": card["number"], "card_number": card["number"],
            "cc-number": card["number"], "cardNum": card["number"],
        }
        exp_fields = {
            "expirationDate": f"{card['exp_month']}/{card['exp_year'][-2:]}",
            "expiration_date": f"{card['exp_month']}/{card['exp_year'][-2:]}",
            "expDate": f"{card['exp_month']}/{card['exp_year'][-2:]}",
            "expiryDate": f"{card['exp_month']}/{card['exp_year'][-2:]}",
        }
        month_fields = {
            "expMonth": card["exp_month"], "exp_month": card["exp_month"],
            "expirationMonth": card["exp_month"],
        }
        year_fields = {
            "expYear": card["exp_year"], "exp_year": card["exp_year"],
            "expirationYear": card["exp_year"],
        }
        cvv_fields = {
            "cvv": card["cvv"], "cvc": card["cvv"], "cvvNumber": card["cvv"],
            "securityCode": card["cvv"],
        }

        for mapping in [card_map, exp_fields, month_fields, year_fields, cvv_fields]:
            for key, value in mapping.items():
                if key in fields:
                    fields[key] = value

        # Billing address
        addr_map = {
            "line1": addr.get("line1", ""), "addressLine1": addr.get("line1", ""),
            "billingLine1": addr.get("line1", ""), "billingAddressLine1": addr.get("line1", ""),
            "streetAddress": addr.get("line1", ""), "address_line1": addr.get("line1", ""),
            "city": addr.get("city", ""), "addressCity": addr.get("city", ""),
            "billingCity": addr.get("city", ""), "billingLocality": addr.get("city", ""),
            "state": addr.get("state", ""), "addressState": addr.get("state", ""),
            "billingState": addr.get("state", ""), "billingAdministrativeArea": addr.get("state", ""),
            "postalCode": addr.get("postal_code", ""), "zip": addr.get("postal_code", ""),
            "postal_code": addr.get("postal_code", ""), "zipCode": addr.get("postal_code", ""),
            "billingPostalCode": addr.get("postal_code", ""),
            "country": "US", "countryCode": "US", "billingCountry": "US", "billingCountryCode": "US",
        }
        for key, value in addr_map.items():
            if key in fields:
                fields[key] = value

        r = self._submit_form(action, fields)
        self._current_url = str(r.url)
        self._current_html = r.text
        self._update_csrf(r)
        self._check_blocked(self._current_html, "card")
        print("[re] Card and billing filled")

    # ──────────────── payment submission ────────────────

    def _submit_payment(self):
        """Submit the final payment / agree to terms."""
        # Look for agree/submit form
        form = self._find_form_by_fields([
            "agree", "terms", "consent", "agreement",
        ])
        if not form:
            # Try finding submit form by button text
            form = self._find_submit_form()

        if form:
            action, fields = form
            # Set agreement checkboxes
            for key in ["agree", "terms", "consent", "agreement", "termsOfService"]:
                if key in fields:
                    fields[key] = "true"

            r = self._submit_form(action, fields)
            self._current_url = str(r.url)
            self._current_html = r.text
            self._update_csrf(r)
            self._check_blocked(self._current_html, "submit")
            print("[re] Payment submitted")
        else:
            # Try POST to current URL as fallback
            r = self._safe_request("POST", self._current_url)
            self._current_url = str(r.url)
            self._current_html = r.text
            print("[re] Payment submitted (direct POST)")

    # ──────────────── auth token extraction ────────────────

    def _extract_auth_tokens(self) -> ReversePayResult:
        """Extract OAuth tokens from the redirect chain after payment."""
        # Follow redirect chain: PayPal → Stripe → ChatGPT
        r = self._follow_redirects(self._current_url, max_hops=10)
        final_url = str(r.url)
        print(f"[*] Final redirect: {final_url[:80]}")

        # Strategy 1: Extract from response cookies
        access_token = ""
        refresh_token = ""
        for name, value in self._cookie_dict(r).items():
            name_lower = name.lower()
            if "session-token" in name_lower or "access" in name_lower:
                if value.startswith("eyJ"):
                    access_token = value
            if "refresh" in name_lower:
                refresh_token = value

        # Strategy 2: Extract from URL fragment or query params
        if not access_token:
            parsed = urlparse(final_url)
            fragment = parsed.fragment or ""
            params = parse_qs(fragment)
            for key in ("access_token", "accessToken"):
                if key in params:
                    access_token = params[key][0]
                    break

        # Strategy 3: Call /api/auth/session with current cookies
        if not access_token:
            auth_result = self._poll_auth_session(r)
            if auth_result:
                access_token = self._extract_token(auth_result, "accessToken", "access_token")
                refresh_token = self._extract_token(auth_result, "refreshToken", "refresh_token")

        if not access_token:
            return ReversePayResult(
                ok=False,
                error="could not extract access_token from redirect chain",
                failed_step="auth_token",
            )

        return ReversePayResult(
            ok=True,
            access_token=access_token,
            oauth_refresh_token=refresh_token,
            refresh_token_status="oauth_present" if refresh_token else "missing",
            redirect_url=final_url,
        )

    def _follow_redirects(self, url: str, max_hops: int = 10) -> Any:
        """Follow redirect chain, accumulating cookies."""
        for _ in range(max_hops):
            r = self._safe_request("GET", url, allow_redirects=False)
            location = r.headers.get("Location") or r.headers.get("location", "")
            if not location or r.status_code not in (301, 302, 303, 307, 308):
                return r
            url = urljoin(url, location)
        return r

    def _poll_auth_session(self, last_response: Any) -> dict | None:
        """Try to get auth session from ChatGPT API."""
        chat_base = CFG.get("chatgpt", {}).get("chat_base_url", "https://chatgpt.com")
        auth_url = f"{chat_base.rstrip('/')}/api/auth/session"

        try:
            r = self._safe_request("GET", auth_url)
            if r.status_code == 200:
                body = r.json()
                if self._extract_token(body, "accessToken", "access_token"):
                    return body
        except Exception as e:
            print(f"[re] Auth session poll failed: {e}")
        return None

    @staticmethod
    def _extract_token(data: dict, *keys: str) -> str:
        if not isinstance(data, dict):
            return ""
        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        session = data.get("session")
        if isinstance(session, dict):
            for key in keys:
                value = session.get(key)
                if isinstance(value, str) and value:
                    return value
        return ""

    # ──────────────── HTML parsing helpers ────────────────

    def _find_signup_form(self) -> tuple[str, dict] | None:
        """Find the main signup/login form on the page."""
        return self._find_form_by_fields([
            "email", "login_email", "signup_email", "login_emailcopy",
        ])

    def _find_submit_form(self) -> tuple[str, dict] | None:
        """Find a form with a submit button containing agree/pay text."""
        html = self._current_html
        # Look for form with submit button
        form_pattern = re.compile(
            r'<form[^>]*>(.*?)</form>',
            re.DOTALL | re.IGNORECASE,
        )
        for match in form_pattern.finditer(html):
            form_html = match.group(0)
            # Check for submit button with relevant text
            if re.search(
                r'(?:agree|pay\s*now|continue|submit|confirm|authorize)',
                form_html, re.I,
            ):
                action = self._extract_form_action(form_html)
                fields = self._extract_hidden_fields(form_html)
                return action, fields
        return None

    def _find_form_by_fields(self, field_names: list[str]) -> tuple[str, dict] | None:
        """Find a form containing any of the specified field names."""
        html = self._current_html
        form_pattern = re.compile(
            r'<form[^>]*>(.*?)</form>',
            re.DOTALL | re.IGNORECASE,
        )
        for match in form_pattern.finditer(html):
            form_html = match.group(0)
            form_lower = form_html.lower()
            for name in field_names:
                if name.lower() in form_lower:
                    action = self._extract_form_action(form_html)
                    fields = self._extract_hidden_fields(form_html)
                    # Also extract input fields
                    fields.update(self._extract_input_fields(form_html))
                    fields.update(self._extract_select_fields(form_html))
                    return action, fields
        return None

    def _extract_form_action(self, form_html: str) -> str:
        """Extract the action URL from a form tag."""
        m = re.search(r'<form[^>]*action=["\']([^"\']*)["\']', form_html, re.I)
        if m:
            action = m.group(1)
            if action.startswith("/"):
                parsed = urlparse(self._current_url)
                return f"{parsed.scheme}://{parsed.netloc}{action}"
            if action.startswith("http"):
                return action
        return self._current_url

    def _extract_hidden_fields(self, form_html: str) -> dict:
        """Extract hidden input fields from a form."""
        fields = {}
        pattern = re.compile(
            r'<input[^>]*type=["\']hidden["\'][^>]*>',
            re.IGNORECASE,
        )
        for match in pattern.finditer(form_html):
            tag = match.group(0)
            name = self._get_attr(tag, "name")
            value = self._get_attr(tag, "value")
            if name:
                fields[name] = value
        return fields

    def _extract_input_fields(self, form_html: str) -> dict:
        """Extract all input fields from a form."""
        fields = {}
        pattern = re.compile(
            r'<input[^>]*>',
            re.IGNORECASE,
        )
        for match in pattern.finditer(form_html):
            tag = match.group(0)
            name = self._get_attr(tag, "name")
            value = self._get_attr(tag, "value")
            input_type = self._get_attr(tag, "type").lower()
            if name and input_type not in ("submit", "button", "image"):
                fields[name] = value
        return fields

    def _extract_select_fields(self, form_html: str) -> dict:
        """Extract select field names with their selected/default option values."""
        fields = {}
        pattern = re.compile(r'<select[^>]*>.*?</select>', re.IGNORECASE | re.DOTALL)
        for match in pattern.finditer(form_html):
            tag = match.group(0)
            name = self._get_attr(tag, "name")
            if not name:
                continue
            selected = re.search(r'<option[^>]*selected[^>]*value=["\']([^"\']*)["\']', tag, re.I)
            if selected:
                fields[name] = selected.group(1)
                continue
            first = re.search(r'<option[^>]*value=["\']([^"\']*)["\']', tag, re.I)
            fields[name] = first.group(1) if first else ""
        return fields

    @staticmethod
    def _get_attr(tag: str, attr: str) -> str:
        """Get an attribute value from an HTML tag."""
        m = re.search(rf'{attr}=["\']([^"\']*)["\']', tag, re.I)
        return m.group(1) if m else ""

    def _is_js_only_page(self) -> bool:
        """Check if the page is JS-only (no parseable forms)."""
        html = self._current_html
        # If there's very little HTML content, it's likely JS-rendered
        if len(html) < 500:
            return True
        # Check for React/Vue/Angular mount points with no content
        if re.search(r'<div id="(app|root|__next)"[^>]*>\s*</div>', html):
            return True
        return False

    def _check_blocked(self, html: str, step: str):
        """Check for CAPTCHA or block pages.

        When CAPTCHA is detected, attempts automatic solving via captcha_solver,
        then submits the token to the challenge endpoint and re-checks the page.
        """
        # Skip CAPTCHA check if nodriver already solved it
        if self._captcha_solved_by_nodriver:
            print(f"[re] CAPTCHA already solved by nodriver, skipping check at {step}")
            return

        for pattern in _CAPTCHA_PATTERNS:
            if pattern.search(html):
                token = self._try_solve_captcha(html, step)
                if token == "__nodriver_cookies__":
                    # nodriver solved CAPTCHA — reload with new cookies and URL
                    r = self._safe_request("GET", self._current_url)
                    self._current_html = r.text
                    self._current_url = str(r.url)
                    self._update_csrf(r)
                    # If nodriver flag is set, trust it and continue
                    if self._captcha_solved_by_nodriver:
                        print(f"[re] CAPTCHA bypassed via nodriver at {step} step")
                        return
                    print(f"[re] nodriver cookies insufficient at {step}, falling back")
                elif token:
                    # Submit token to challenge endpoint and re-request
                    self._submit_captcha_challenge(html, token)
                    # Re-request the current page to verify CAPTCHA is cleared
                    r = self._safe_request("GET", self._current_url)
                    self._current_html = r.text
                    self._current_url = str(r.url)
                    self._update_csrf(r)
                    still_blocked = any(p.search(self._current_html) for p in _CAPTCHA_PATTERNS)
                    if not still_blocked:
                        print(f"[re] CAPTCHA solved at {step} step, page reloaded")
                        return
                    print(f"[re] CAPTCHA persists at {step} after solve, falling back")
                raise _NeedBrowserFallback(step, f"CAPTCHA detected at {step} step")
        for pattern in _BLOCK_PATTERNS:
            if pattern.search(html):
                raise _NeedBrowserFallback(step, f"page blocked at {step} step")

    def _submit_captcha_challenge(self, html: str, token: str):
        """Submit solved CAPTCHA token to PayPal's challenge endpoint.

        PayPal's ADS CAPTCHA expects the token to be POSTed to the challenge
        form action URL (typically /auth/createchallenge/.../challenge).
        """
        # Extract challenge form action
        action_match = re.search(
            r'<form[^>]*name=["\']?challenge["\']?[^>]*action=["\']([^"\']+)["\']',
            html, re.I,
        )
        if not action_match:
            # Try alternative patterns
            action_match = re.search(
                r'action=["\']([^"\']*(?:challenge|auth)[^"\']*)["\']',
                html, re.I,
            )
        if not action_match:
            print("[re] No challenge form action found, skipping challenge submit")
            return

        action = action_match.group(1)
        if action.startswith("/"):
            parsed = urlparse(self._current_url)
            action = f"{parsed.scheme}://{parsed.netloc}{action}"

        # Extract hidden fields from the challenge form
        form_match = re.search(
            r'<form[^>]*name=["\']?challenge["\']?[^>]*>(.*?)</form>',
            html, re.DOTALL | re.I,
        )
        fields = {}
        if form_match:
            form_html = form_match.group(1)
            for m in re.finditer(r'<input[^>]*type=["\']hidden["\'][^>]*>', form_html, re.I):
                tag = m.group(0)
                name = self._get_attr(tag, "name")
                value = self._get_attr(tag, "value")
                if name:
                    fields[name] = value

        # Add CAPTCHA response token
        fields["g-recaptcha-response"] = token
        fields["h-captcha-response"] = token
        if self._captcha_ekey:
            fields["recaptcha-ekey"] = self._captcha_ekey

        headers = {
            "Referer": self._current_url,
            "Origin": urlparse(self._current_url).scheme + "://" + urlparse(self._current_url).netloc,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
        }

        try:
            r = self._safe_request("POST", action, data=fields, headers=headers)
            # Update cookies from challenge response
            print(f"[re] Challenge submit: {r.status_code} {str(r.url)[:60]}")
        except Exception as e:
            print(f"[re] Challenge submit failed: {e}")

    def _try_solve_captcha(self, html: str, step: str) -> str | None:
        """Attempt to solve CAPTCHA automatically. Returns token or None.

        Strategy:
        1. Try basic Playwright bridge solver (invisible v3 / simple v2)
        2. If that fails, load the real PayPal page in browser and solve v2
        """
        try:
            from .captcha_solver import (
                CaptchaError,
                _PAYPAL_RECAPTCHA_SITE_KEY,
                _solve_hcaptcha,
                _solve_recaptcha,
                extract_captcha_config,
                solve_recaptcha_on_page,
            )
        except ImportError:
            print("[re] captcha_solver module not available")
            return None

        config = extract_captcha_config(html)
        captcha_type = config.get("type", "")
        site_key = config.get("site_key", "")

        if not site_key:
            if re.search(r"captcha", html, re.I) and _PAYPAL_RECAPTCHA_SITE_KEY:
                print(f"[re] CAPTCHA at {step}, using PayPal reCAPTCHA fallback key")
                captcha_type = "recaptcha"
                site_key = _PAYPAL_RECAPTCHA_SITE_KEY
            else:
                print(f"[re] CAPTCHA detected at {step} but no site_key found")
                return None

        # Step 1: Try basic bridge solver
        try:
            if captcha_type == "hcaptcha":
                token, ekey = _solve_hcaptcha(
                    site_key=site_key,
                    rqdata=config.get("rqdata", ""),
                    proxy=self.proxy or "",
                    headless=True,
                    timeout_ms=90000,
                    locale="en-US",
                    log=lambda msg: print(msg),
                )
            else:
                token, ekey = _solve_recaptcha(
                    site_key=site_key,
                    proxy=self.proxy or "",
                    headless=True,
                    timeout_ms=90000,
                    locale="en-US",
                    log=lambda msg: print(msg),
                )
            if token:
                self._captcha_token = token
                self._captcha_ekey = ekey
                return token
        except CaptchaError as e:
            print(f"[re] Basic solver failed at {step}: {e}")
        except Exception as e:
            print(f"[re] Basic solver error at {step}: {e}")

        # Step 2: Browser fallback - load real page and solve reCAPTCHA v2
        print(f"[re] Trying browser fallback for CAPTCHA at {step}...")
        try:
            # Collect cookies from the HTTP session
            session_cookies = {}
            if hasattr(self._session, "cookies"):
                jar = self._session.cookies
                if hasattr(jar, "get_dict"):
                    session_cookies = jar.get_dict()
                else:
                    session_cookies = dict(jar)

            token, ekey = solve_recaptcha_on_page(
                page_url=self._current_url,
                cookies=session_cookies,
                proxy=self.proxy or "",
                headless=True,
                timeout_ms=120000,
                locale="en-US",
                log=lambda msg: print(msg),
            )
            if token:
                self._captcha_token = token
                self._captcha_ekey = ekey
                return token
        except CaptchaError as e:
            print(f"[re] Browser fallback failed at {step}: {e}")
        except Exception as e:
            print(f"[re] Browser fallback error at {step}: {e}")

        # Step 3: nodriver fallback (undetected Chrome)
        print(f"[re] Trying nodriver for CAPTCHA at {step}...")
        try:
            from .nodriver_captcha import solve_captcha_with_nodriver

            nd_result = solve_captcha_with_nodriver(
                page_url=self._current_url,
                proxy=self.proxy.replace("socks5h://", "socks5://") if self.proxy else "",
                headless=False,
                timeout=120,
            )
            if nd_result.get("ok") and nd_result.get("cookies"):
                # Import nodriver cookies into HTTP session
                for name, value in nd_result["cookies"].items():
                    self._session.cookies.set(name, value, domain=".paypal.com")
                print(f"[re] Imported {len(nd_result['cookies'])} nodriver cookies")
                self._nodriver_cookies = nd_result["cookies"]

                # If nodriver navigated past CAPTCHA, use its final URL
                final_url = nd_result.get("final_url", "")
                if final_url and "paypal.com" in final_url:
                    self._current_url = final_url
                    print(f"[re] Using nodriver final URL: {final_url[:80]}")

                # Mark CAPTCHA as solved so _check_blocked skips re-check
                self._captcha_solved_by_nodriver = True
                return "__nodriver_cookies__"
        except Exception as e:
            print(f"[re] nodriver fallback failed at {step}: {e}")

        return None

    def _update_csrf(self, response: Any):
        """Extract CSRF token from response."""
        html = response.text if hasattr(response, "text") else ""
        # Meta tag
        m = re.search(r'<meta[^>]*name=["\']csrf[_-]?token["\'][^>]*content=["\']([^"\']*)["\']', html, re.I)
        if m:
            self._csrf_token = m.group(1)
            return
        # Hidden input
        m = re.search(r'<input[^>]*name=["\']_token["\'][^>]*value=["\']([^"\']*)["\']', html, re.I)
        if m:
            self._csrf_token = m.group(1)
            return
        # Set-Cookie header
        for name, value in self._cookie_dict(response).items():
            if "csrf" in name.lower() or "xsrftoken" in name.lower():
                self._csrf_token = value
                return

    # ──────────────── cookie helpers ────────────────────────────

    @staticmethod
    def _cookie_dict(response: Any) -> dict[str, str]:
        """Return {name: value} from response cookies, compatible with requests and curl_cffi.

        requests.RequestsCookieJar has get_dict() returning {str: str}.
        curl_cffi.Cookies inherits from dict, so dict() gives {str: str} directly.
        """
        jar = response.cookies
        if hasattr(jar, "get_dict"):
            return jar.get_dict()
        return dict(jar)

    # ──────────────── HTTP helpers ────────────────

    def _submit_form(self, action: str, fields: dict) -> Any:
        """Submit a form via POST with CSRF token."""
        headers = {
            "Referer": self._current_url,
            "Origin": urlparse(self._current_url).scheme + "://" + urlparse(self._current_url).netloc,
            "Content-Type": "application/x-www-form-urlencoded",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "X-Requested-With": "XMLHttpRequest",
        }
        if self._csrf_token:
            headers["X-CSRF-Token"] = self._csrf_token

        # Remove None/empty values
        clean_fields = {k: v for k, v in fields.items() if v is not None}

        # Inject CAPTCHA token if available
        if self._captcha_token:
            clean_fields["g-recaptcha-response"] = self._captcha_token
            clean_fields["h-captcha-response"] = self._captcha_token
            if self._captcha_ekey:
                clean_fields["recaptcha-ekey"] = self._captcha_ekey
            # Clear after use to avoid stale tokens
            self._captcha_token = ""
            self._captcha_ekey = ""

        r = self._safe_request("POST", action, data=clean_fields, headers=headers)
        return r

    def _safe_request(self, method: str, url: str, **kwargs) -> Any:
        """Make an HTTP request with error handling."""
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("allow_redirects", True)
        try:
            r = self._session.request(method, url, **kwargs)
            return r
        except Exception as e:
            raise _NeedBrowserFallback("request", f"{method} {url[:60]} failed: {e}")


# ──────────────────────────── exception ────────────────────────────


class _NeedBrowserFallback(Exception):
    """Raised when the reverse-engineered flow cannot continue."""

    def __init__(self, step: str, detail: str):
        self.step = step
        self.detail = detail
        super().__init__(f"[{step}] {detail}")


# ──────────────────────────── public helper ────────────────────────────


def try_reverse_pay(
    redirect_url: str,
    card: dict,
    address: dict,
    first_name: str,
    last_name: str,
    alias_email: str,
    password: str,
    phone: str,
    sms_cfg: dict,
    proxy: str | None = None,
    cookie_header: str = "",
    timeout: int = 60,
) -> dict[str, Any]:
    """Convenience wrapper for PayPalReverseClient."""
    client = PayPalReverseClient(
        redirect_url=redirect_url,
        card=card,
        address=address,
        first_name=first_name,
        last_name=last_name,
        alias_email=alias_email,
        password=password,
        phone=phone,
        sms_cfg=sms_cfg,
        proxy=proxy,
        cookie_header=cookie_header,
        timeout=timeout,
    )
    result = client.authorize()
    return result.to_dict()
