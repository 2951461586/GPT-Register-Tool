(function () {
  "use strict";

  const DEFAULT_PROFILE = window.PAYPAL_AUTOFILL_PROFILE || {};
  const STORAGE_KEY = "paypalAutofillProfile";
  const STATE_KEY = "paypalAutofillState";
  const LOG_PREFIX = "[GPT PayPal Autofill]";
  const OTP_POLL_INTERVAL_MS = 2000;
  const OTP_POLL_ATTEMPTS = 12;

  const SELECTORS = {
    email: ["#email", "input[name='email']", "input[type='email']", "input[autocomplete='email']"],
    phone: ["#phone", "#phoneNumber", "input[name='phone']", "input[name='phoneNumber']", "input[type='tel']", "input[autocomplete='tel']"],
    password: ["#password", "input[name='password']", "input[type='password']", "input[autocomplete='new-password']"],
    firstName: ["#firstName", "input[name='firstName']", "input[name='first_name']", "input[autocomplete='given-name']"],
    lastName: ["#lastName", "input[name='lastName']", "input[name='last_name']", "input[autocomplete='family-name']"],
    cardNumber: ["#cardNumber", "input[name='cardNumber']", "input[name='cardnumber']", "input[autocomplete='cc-number']", "input[aria-label*='card number' i]"],
    cardExpiry: ["#cardExpiry", "input[name='cardExpiry']", "input[name='expiry']", "input[autocomplete='cc-exp']", "input[aria-label*='expiration' i]"],
    cardCvv: ["#cardCvv", "#cvv", "#cvc", "input[name='cardCvv']", "input[name='cvv']", "input[name='cvc']", "input[autocomplete='cc-csc']", "input[aria-label*='security' i]"],
    line1: ["#billingLine1", "#billingAddressLine1", "#addressLine1", "input[name='billingLine1']", "input[name='billingAddressLine1']", "input[autocomplete='address-line1']"],
    city: ["#billingCity", "#billingLocality", "#city", "input[name='billingCity']", "input[name='billingLocality']", "input[autocomplete='address-level2']"],
    postalCode: ["#billingPostalCode", "#postalCode", "#zip", "input[name='billingPostalCode']", "input[name='postalCode']", "input[autocomplete='postal-code']"],
    state: ["#billingState", "#billingAdministrativeArea", "#state", "select[name*='state' i]", "select[name*='administrative' i]", "input[name*='state' i]"],
    country: ["#country", "select[name='country']", "select[autocomplete='country']", "input[name='country']", "input[autocomplete='country']"]
  };

  const BUTTON_WORDS = ["continue", "next", "agree", "submit", "confirm", "pay", "subscribe", "done"];

  function log(...args) {
    console.log(LOG_PREFIX, ...args);
  }

  function storageGet(keys) {
    return new Promise((resolve) => chrome.storage.local.get(keys, resolve));
  }

  function storageSet(value) {
    return new Promise((resolve) => chrome.storage.local.set(value, resolve));
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function normalizeProfile(profile = {}) {
    const card = profile.card || {};
    const address = profile.address || {};
    return {
      enabled: profile.enabled !== false,
      email: profile.email || "",
      phone: profile.phone || "",
      phonePool: Array.isArray(profile.phonePool) ? profile.phonePool : [],
      otpUrl: profile.otpUrl || "",
      password: profile.password || "",
      firstName: profile.firstName || "",
      lastName: profile.lastName || "",
      card: {
        number: card.number || profile.cardNumber || "",
        expiry: card.expiry || profile.cardExpiry || "",
        cvv: card.cvv || profile.cardCvv || ""
      },
      cardPool: Array.isArray(profile.cardPool) ? profile.cardPool : [],
      address: {
        line1: address.line1 || profile.addressLine1 || "",
        city: address.city || profile.city || "",
        state: address.state || profile.state || "",
        postalCode: address.postalCode || profile.postalCode || "",
        country: address.country || profile.country || "US"
      }
    };
  }

  async function readProfile() {
    const data = await storageGet([STORAGE_KEY, STATE_KEY]);
    const stored = normalizeProfile(data[STORAGE_KEY] || {});
    const base = normalizeProfile({ ...DEFAULT_PROFILE, ...stored });
    const state = data[STATE_KEY] || {};

    const cardPool = base.cardPool.length ? base.cardPool : (base.card.number ? [base.card] : []);
    const phonePool = base.phonePool.length ? base.phonePool : (base.phone ? [base.phone] : []);
    if (cardPool.length) {
      const card = cardPool[Math.abs(Number(state.cardIndex || 0)) % cardPool.length];
      base.card = { ...base.card, ...card };
    }
    if (phonePool.length) {
      base.phone = phonePool[Math.abs(Number(state.phoneIndex || 0)) % phonePool.length];
    }
    return { profile: base, state };
  }

  async function fetchUsAddress() {
    try {
      const response = await chrome.runtime.sendMessage({ type: "FETCH_US_ADDRESS" });
      return response?.ok ? response.address || {} : {};
    } catch (_) {
      return {};
    }
  }

  function needsAddress(profile) {
    const address = profile?.address || {};
    return !address.line1 || !address.city || !address.state || !address.postalCode;
  }

  async function resolveAddress(profile) {
    const fetched = needsAddress(profile) ? await fetchUsAddress() : {};
    return {
      line1: profile.address.line1 || fetched.line1 || "",
      city: profile.address.city || fetched.city || "",
      state: profile.address.state || fetched.state || "",
      postalCode: profile.address.postalCode || fetched.postalCode || "",
      country: profile.address.country || fetched.country || "US"
    };
  }

  async function advancePools({ card = false, phone = false } = {}) {
    const data = await storageGet([STORAGE_KEY, STATE_KEY]);
    const profile = normalizeProfile(data[STORAGE_KEY] || {});
    const state = data[STATE_KEY] || {};
    const nextState = { ...state };

    if (card) {
      const pool = profile.cardPool.length ? profile.cardPool : (profile.card?.number ? [profile.card] : []);
      if (pool.length > 1) nextState.cardIndex = (Number(state.cardIndex || 0) + 1) % pool.length;
    }
    if (phone) {
      const pool = profile.phonePool.length ? profile.phonePool : (profile.phone ? [profile.phone] : []);
      if (pool.length > 1) nextState.phoneIndex = (Number(state.phoneIndex || 0) + 1) % pool.length;
    }

    if (JSON.stringify(nextState) !== JSON.stringify(state)) {
      await storageSet({ [STATE_KEY]: nextState });
    }
    return { profile, state: nextState };
  }

  function isVisible(el) {
    if (!el || el.disabled || el.readOnly) return false;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  }

  function candidates(selectors) {
    return selectors.flatMap((selector) => Array.from(document.querySelectorAll(selector)));
  }

  function nativeSet(el, value) {
    if (!el || value == null || String(value).trim() === "") return false;
    if (el instanceof HTMLSelectElement) return setSelect(el, value);
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, String(value));
    else el.value = String(value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
    return true;
  }

  function setSelect(el, wanted) {
    const text = String(wanted || "").toLowerCase();
    const option = Array.from(el.options || []).find((item) => {
      const label = `${item.textContent || ""} ${item.value || ""}`.toLowerCase();
      return label.includes(text);
    });
    if (!option) return false;
    el.value = option.value;
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }

  function fillFirst(name, selectors, value) {
    if (!value) return false;
    for (const el of candidates(selectors)) {
      if (!isVisible(el)) continue;
      if (nativeSet(el, value)) {
        log("filled", name);
        return true;
      }
    }
    return false;
  }

  function textHints(el) {
    const hints = [
      el.id,
      el.name,
      el.placeholder,
      el.getAttribute("aria-label"),
      el.getAttribute("autocomplete"),
      el.labels?.[0]?.textContent
    ];
    let parent = el.parentElement;
    for (let i = 0; parent && i < 2; i += 1) {
      hints.push(parent.textContent);
      parent = parent.parentElement;
    }
    return hints.filter(Boolean).join(" ").replace(/\s+/g, " ").toLowerCase();
  }

  function fillOtp(code) {
    const digits = String(code || "").replace(/\D/g, "");
    if (!digits) return false;
    const inputs = Array.from(document.querySelectorAll("input")).filter((input) => {
      if (!isVisible(input)) return false;
      const max = Number(input.getAttribute("maxlength") || input.maxLength || 0);
      const hints = textHints(input);
      const type = String(input.type || "").toLowerCase();
      const mode = String(input.getAttribute("inputmode") || "").toLowerCase();
      if (/zip|postal|card|cvv|cvc|phone|email|name|address|city|state/i.test(hints)) return false;
      return input.autocomplete === "one-time-code" ||
        /otp|verification|security code|one.?time|passcode|code/.test(hints) ||
        ((max >= 4 && max <= 8) && /text|tel|number|password/.test(type || "text")) ||
        (max === 1 && /numeric|decimal|tel/.test(mode || type));
    });
    if (!inputs.length) return false;
    if (inputs.length === 1) {
      inputs[0].focus();
      return nativeSet(inputs[0], digits);
    }
    inputs.slice(0, digits.length).forEach((input, index) => {
      input.focus();
      nativeSet(input, digits[index]);
    });
    return true;
  }

  function clickContinue() {
    const buttons = Array.from(document.querySelectorAll("button, input[type='button'], input[type='submit']"));
    const target = buttons.find((button) => {
      if (!isVisible(button)) return false;
      const text = `${button.textContent || ""} ${button.value || ""} ${button.getAttribute("aria-label") || ""}`.toLowerCase();
      return BUTTON_WORDS.some((word) => text.includes(word));
    });
    if (!target) return false;
    target.click();
    return true;
  }

  function clickPayPalMethod() {
    const buttons = candidates([
      "[data-testid='paypal-accordion-item-button']",
      ".paypal-accordion-item button",
      "button"
    ]);
    const target = buttons.find((button) => isVisible(button) && /paypal/i.test(button.textContent || button.getAttribute("aria-label") || ""));
    if (!target) return false;
    target.click();
    return true;
  }

  function checkTerms() {
    const box = candidates(["#termsOfServiceConsentCheckbox", "input[type='checkbox'][name*='terms' i]", "input[type='checkbox']"])
      .find((item) => isVisible(item) && !item.checked);
    if (!box) return false;
    box.click();
    return true;
  }

  async function fillForms({ clickNext = false, useAddress = true } = {}) {
    const { profile } = await readProfile();
    if (!profile.enabled) return { ok: false, message: "disabled" };

    let filled = 0;
    const add = (ok) => { if (ok) filled += 1; };
    const address = useAddress ? await resolveAddress(profile) : profile.address;
    clickPayPalMethod();
    add(fillFirst("email", SELECTORS.email, profile.email));
    add(fillFirst("phone", SELECTORS.phone, formatPhone(profile.phone)));
    add(fillFirst("password", SELECTORS.password, profile.password));
    add(fillFirst("firstName", SELECTORS.firstName, profile.firstName));
    add(fillFirst("lastName", SELECTORS.lastName, profile.lastName));
    add(fillFirst("cardNumber", SELECTORS.cardNumber, profile.card.number));
    add(fillFirst("cardExpiry", SELECTORS.cardExpiry, profile.card.expiry));
    add(fillFirst("cardCvv", SELECTORS.cardCvv, profile.card.cvv));
    add(fillFirst("address.line1", SELECTORS.line1, address.line1));
    add(fillFirst("address.city", SELECTORS.city, address.city));
    add(fillFirst("address.postalCode", SELECTORS.postalCode, address.postalCode));
    add(fillFirst("address.state", SELECTORS.state, address.state));
    add(fillFirst("address.country", SELECTORS.country, address.country));
    checkTerms();
    if (clickNext) clickContinue();
    return { ok: filled > 0, message: `filled ${filled} field(s)`, filled };
  }

  function formatPhone(phone) {
    const raw = String(phone || "").trim();
    const digits = raw.replace(/\D/g, "");
    if (digits.length === 11 && digits.startsWith("1")) return digits.slice(1);
    return raw;
  }

  async function pollOtpCode(url, attempts = OTP_POLL_ATTEMPTS) {
    for (let i = 0; i < attempts; i += 1) {
      const response = await chrome.runtime.sendMessage({ type: "FETCH_OTP_SMS", url });
      const code = extractCode(response?.text || response?.error || "");
      if (code) return code;
      if (i < attempts - 1) await sleep(OTP_POLL_INTERVAL_MS);
    }
    return "";
  }

  async function fillOtpFromProfile({ submit = true, poll = true } = {}) {
    const { profile } = await readProfile();
    if (!profile.otpUrl) return { ok: false, message: "otp url missing" };
    const code = poll ? await pollOtpCode(profile.otpUrl) : extractCode((await chrome.runtime.sendMessage({ type: "FETCH_OTP_SMS", url: profile.otpUrl }))?.text || "");
    if (!code) return { ok: false, message: "otp not found" };
    const ok = fillOtp(code);
    if (ok && submit) clickContinue();
    return { ok, code, message: ok ? `otp ${code}` : "otp input not found" };
  }

  async function runFullFlow() {
    const fillResult = await fillForms({ clickNext: true, useAddress: true });
    if (!fillResult.ok) return fillResult;
    const otpResult = await fillOtpFromProfile({ submit: true, poll: true });
    if (otpResult.ok) {
      await advancePools({ card: true, phone: true });
      return { ok: true, message: `filled + otp ${otpResult.code}` };
    }
    return otpResult;
  }

  function ensureMiniPanel() {
    if (document.getElementById("gpt-paypal-autofill-panel")) return;
    const style = document.createElement("style");
    style.textContent = `
      #gpt-paypal-autofill-panel{position:fixed;right:16px;top:96px;z-index:2147483647;width:178px;padding:10px;border:1px solid #244239;border-radius:8px;background:#08110f;color:#dffcef;font:12px/1.3 ui-monospace,Consolas,monospace;box-shadow:0 18px 48px rgba(0,0,0,.36)}
      #gpt-paypal-autofill-panel strong{display:block;margin-bottom:8px;color:#31f296;font-size:12px;letter-spacing:0}
      #gpt-paypal-autofill-panel button{width:100%;height:28px;margin-top:6px;border:1px solid #31594d;border-radius:6px;background:#0e1a17;color:#dffcef;font:inherit;cursor:pointer}
      #gpt-paypal-autofill-panel button:hover{border-color:#31f296;color:#31f296}
      #gpt-paypal-autofill-panel [data-close]{position:absolute;right:6px;top:6px;width:22px;height:22px;margin:0}
      #gpt-paypal-autofill-panel [data-state]{min-height:16px;margin-top:8px;color:#8db7aa;word-break:break-all}
    `;
    const panel = document.createElement("section");
    panel.id = "gpt-paypal-autofill-panel";
    panel.innerHTML = `
      <button data-close type="button">x</button>
      <strong>PAY FILLER</strong>
      <button data-fill type="button">Fill form</button>
      <button data-run type="button">Run all</button>
      <button data-otp type="button">Fill OTP</button>
      <button data-submit type="button">Continue</button>
      <div data-state>ready</div>`;
    document.documentElement.append(style, panel);
    const state = panel.querySelector("[data-state]");
    panel.querySelector("[data-close]").addEventListener("click", () => panel.remove());
    panel.querySelector("[data-fill]").addEventListener("click", async () => {
      const result = await fillForms();
      state.textContent = result.message;
    });
    panel.querySelector("[data-run]").addEventListener("click", async () => {
      state.textContent = "running...";
      const result = await runFullFlow();
      state.textContent = result.message;
    });
    panel.querySelector("[data-otp]").addEventListener("click", async () => {
      const result = await fillOtpFromProfile({ submit: false, poll: true });
      state.textContent = result.message;
      if (result.ok) fillOtp(result.code);
    });
    panel.querySelector("[data-submit]").addEventListener("click", () => {
      state.textContent = clickContinue() ? "continued" : "button not found";
    });
  }

  function extractCode(text) {
    const match = String(text || "").match(/(?<!\d)\d{4,8}(?!\d)/);
    return match ? match[0] : "";
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "PAYPAL_AUTOFILL_TOGGLE_PANEL") {
      const panel = document.getElementById("gpt-paypal-autofill-panel");
      if (panel) panel.remove();
      else ensureMiniPanel();
      sendResponse({ ok: true });
      return true;
    }
    if (message?.type === "PAYPAL_AUTOFILL_FILL") {
      fillForms({ clickNext: Boolean(message.clickNext), useAddress: message.useAddress !== false }).then(sendResponse);
      return true;
    }
    if (message?.type === "PAYPAL_AUTOFILL_RUN_ALL") {
      runFullFlow().then(sendResponse);
      return true;
    }
    if (message?.type === "PAYPAL_AUTOFILL_FILL_OTP") {
      const ok = fillOtp(message.code);
      if (ok && message.submit) clickContinue();
      sendResponse({ ok, message: ok ? "otp filled" : "otp input not found" });
      return true;
    }
    if (message?.type === "PAYPAL_AUTOFILL_CONTINUE") {
      const ok = clickContinue();
      sendResponse({ ok, message: ok ? "continued" : "button not found" });
      return true;
    }
    return false;
  });

  if (DEFAULT_PROFILE.enabled) {
    setInterval(() => fillForms().catch(() => {}), 1800);
    setTimeout(() => fillForms().catch((error) => log("fill error", error)), 500);
  }
})();
