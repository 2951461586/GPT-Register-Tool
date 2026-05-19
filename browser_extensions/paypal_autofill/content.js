(function () {
  "use strict";

  const profile = window.PAYPAL_AUTOFILL_PROFILE || {};
  if (!profile.enabled) return;

  const log = (...args) => console.log("[GPT PayPal Autofill]", ...args);
  const filled = new WeakSet();
  let lastRun = 0;

  function value(path, fallback = "") {
    return path.split(".").reduce((cur, key) => (cur && cur[key] != null ? cur[key] : ""), profile) || fallback;
  }

  function setNativeValue(el, val) {
    if (!el || val == null || String(val).length === 0) return false;
    const tag = (el.tagName || "").toLowerCase();
    const proto = tag === "textarea" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, String(val));
    else el.value = String(val);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
    filled.add(el);
    return true;
  }

  function setSelect(el, wanted) {
    if (!el || !wanted) return false;
    const text = String(wanted).toLowerCase();
    for (const option of el.options || []) {
      const label = `${option.textContent || ""} ${option.value || ""}`.toLowerCase();
      if (label.includes(text)) {
        el.value = option.value;
        el.dispatchEvent(new Event("change", { bubbles: true }));
        return true;
      }
    }
    return false;
  }

  function candidates(selectors) {
    return selectors.flatMap((selector) => Array.from(document.querySelectorAll(selector)));
  }

  function fillFirst(name, selectors, val) {
    if (!val) return false;
    for (const el of candidates(selectors)) {
      if (filled.has(el)) continue;
      if (el.offsetParent === null && el.getClientRects().length === 0) continue;
      if (setNativeValue(el, val)) {
        log("filled", name);
        return true;
      }
    }
    return false;
  }

  function clickFirst(name, selectors, textMatchers) {
    const elements = candidates(selectors);
    for (const el of elements) {
      if (el.dataset.gptAutofillClicked === "1") continue;
      const text = (el.textContent || el.getAttribute("aria-label") || "").trim().toLowerCase();
      if (textMatchers.length && !textMatchers.some((matcher) => text.includes(matcher))) continue;
      if (el.disabled || (el.offsetParent === null && el.getClientRects().length === 0)) continue;
      el.dataset.gptAutofillClicked = "1";
      el.click();
      log("clicked", name);
      return true;
    }
    return false;
  }

  function fillState() {
    const state = value("address.state");
    if (!state) return;
    for (const el of candidates(["#billingState", "#billingAdministrativeArea", "#state", "#billing_state", "select[name*='state' i]", "select[name*='administrative' i]"])) {
      if (setSelect(el, state)) {
        log("selected state");
        return;
      }
    }
  }

  function fillForms() {
    fillFirst("email", ["#email", "input[name='email']", "input[type='email']", "input[autocomplete='email']"], value("email"));
    fillFirst("phone", ["#phone", "#phoneNumber", "input[name='phone']", "input[name='phoneNumber']", "input[type='tel']", "input[autocomplete='tel']"], value("phone"));
    fillFirst("password", ["#password", "input[name='password']", "input[type='password']", "input[autocomplete='new-password']"], value("password"));
    fillFirst("firstName", ["#firstName", "input[name='firstName']", "input[name='first_name']", "input[autocomplete='given-name']"], value("firstName"));
    fillFirst("lastName", ["#lastName", "input[name='lastName']", "input[name='last_name']", "input[autocomplete='family-name']"], value("lastName"));

    fillFirst("card number", ["#cardNumber", "input[name='cardNumber']", "input[name='cardnumber']", "input[autocomplete='cc-number']", "input[aria-label*='card number' i]"], value("card.number"));
    fillFirst("card expiry", ["#cardExpiry", "input[name='cardExpiry']", "input[name='expiry']", "input[autocomplete='cc-exp']", "input[aria-label*='expiration' i]"], value("card.expiry"));
    fillFirst("card cvv", ["#cardCvv", "#cvv", "#cvc", "input[name='cardCvv']", "input[name='cvv']", "input[name='cvc']", "input[autocomplete='cc-csc']", "input[aria-label*='security' i]"], value("card.cvv"));

    fillFirst("address line1", ["#billingLine1", "#billingAddressLine1", "#addressLine1", "input[name='billingLine1']", "input[name='billingAddressLine1']", "input[autocomplete='address-line1']"], value("address.line1"));
    fillFirst("city", ["#billingCity", "#billingLocality", "#city", "input[name='billingCity']", "input[name='billingLocality']", "input[autocomplete='address-level2']"], value("address.city"));
    fillFirst("postalCode", ["#billingPostalCode", "#postalCode", "#zip", "input[name='billingPostalCode']", "input[name='postalCode']", "input[autocomplete='postal-code']"], value("address.postalCode"));
    fillState();

    const country = candidates(["#country", "select[name='country']", "select[autocomplete='country']"])[0];
    if (country) setSelect(country, value("address.country", "US"));

    const tos = candidates(["#termsOfServiceConsentCheckbox", "input[type='checkbox'][name*='terms' i]"])[0];
    if (tos && !tos.checked) {
      tos.click();
      log("checked terms box");
    }
  }

  function prepareCheckout() {
    const host = location.host;
    if (host.includes("checkout.stripe.com") || host.includes("pay.openai.com")) {
      clickFirst("PayPal payment method", [
        "[data-testid='paypal-accordion-item-button']",
        ".paypal-accordion-item button",
        "button"
      ], ["paypal"]);
    }
    if (host.includes("paypal.com")) {
      clickFirst("create account", [
        "a",
        "button"
      ], ["create an account", "sign up", "创建"]);
    }
  }

  function run() {
    const now = Date.now();
    if (now - lastRun < 800) return;
    lastRun = now;
    try {
      prepareCheckout();
      fillForms();
    } catch (error) {
      log("error", error);
    }
  }

  run();
  setInterval(run, 1800);
  new MutationObserver(run).observe(document.documentElement, { childList: true, subtree: true });
})();
