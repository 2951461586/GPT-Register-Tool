(function () {
  "use strict";

  const DEFAULT_PROFILE = window.PAYPAL_AUTOFILL_PROFILE || {};
  const STORAGE_KEY = "paypalAutofillProfile";
  const STATE_KEY = "paypalAutofillState";
  const RUNTIME_KEY = "paypalAutofillRuntime";
  const LOG_PREFIX = "[GPT PayPal Flow]";
  const OTP_POLL_INTERVAL_MS = 2000;
  const OTP_POLL_ATTEMPTS = 15;
  const OTP_FILL_REQUEST_KEY = "paypalAutofillOtpFillRequest";
  const SAVED_OTP_KEY = "paypalAutofillSavedOtp";
  const OTP_FRAME_RETRY_ATTEMPTS = 90;
  const OTP_FRAME_RETRY_DELAY_MS = 500;
  const OTP_REQUEST_MAX_AGE_MS = 10 * 60 * 1000;
  const FLOW_TICK_DELAY_MS = 850;
  const AUTO_RUN_DELAY_MS = 2000;
  const BUTTON_RETRY_DELAY_MS = 1000;

  const STAGES = {
    IDLE: "idle",
    DETECT: "detect",
    OPENAI_CHECKOUT: "openai_checkout",
    OPENAI_BILLING: "openai_billing",
    OPENAI_SUBMIT: "openai_submit",
    PAYPAL_LOGIN_EMAIL: "paypal_login_email",
    PAYPAL_LOGIN_PASSWORD: "paypal_login_password",
    PAYPAL_GUEST: "paypal_guest",
    PAYPAL_SMS: "paypal_sms",
    PAYPAL_REVIEW: "paypal_review",
    PAYPAL_APPROVE: "paypal_approve",
    DONE: "done",
    BLOCKED: "blocked",
    UNKNOWN: "unknown",
    ERROR: "error"
  };

  const LISTENER_SENTINEL = "data-gpt-paypal-state-machine";
  if (document.documentElement.getAttribute(LISTENER_SENTINEL) === "1") {
    return;
  }
  document.documentElement.setAttribute(LISTENER_SENTINEL, "1");
  const hiddenStyle = document.createElement("style");
  hiddenStyle.textContent = "#captcha-standalone,.captcha-overlay,.captcha-container,.AddressAutocomplete-results{display:none!important;height:0!important;overflow:hidden!important}";
  document.documentElement.appendChild(hiddenStyle);

  const STAGE_LABELS = {
    [STAGES.IDLE]: "就绪",
    [STAGES.DETECT]: "识别页面",
    [STAGES.OPENAI_CHECKOUT]: "选择 PayPal",
    [STAGES.OPENAI_BILLING]: "填写账单",
    [STAGES.OPENAI_SUBMIT]: "提交 Stripe",
    [STAGES.PAYPAL_LOGIN_EMAIL]: "PayPal 邮箱",
    [STAGES.PAYPAL_LOGIN_PASSWORD]: "PayPal 密码",
    [STAGES.PAYPAL_GUEST]: "PayPal 填表",
    [STAGES.PAYPAL_SMS]: "短信验证",
    [STAGES.PAYPAL_REVIEW]: "PayPal 审核",
    [STAGES.PAYPAL_APPROVE]: "授权确认",
    [STAGES.DONE]: "完成",
    [STAGES.BLOCKED]: "需要人工处理",
    [STAGES.UNKNOWN]: "未知页面",
    [STAGES.ERROR]: "异常"
  };

  const SELECTORS = {
    email: ["#email", "input[name='email']", "input[type='email']", "input[autocomplete='email']"],
    phone: ["#phone", "#phoneNumber", "input[name='phone']", "input[name='phoneNumber']", "input[type='tel']", "input[autocomplete='tel']"],
    password: ["#password", "input[name='password']", "input[type='password']", "input[autocomplete='current-password']", "input[autocomplete='new-password']"],
    firstName: ["#firstName", "input[name='firstName']", "input[name='first_name']", "input[autocomplete='given-name']"],
    lastName: ["#lastName", "input[name='lastName']", "input[name='last_name']", "input[autocomplete='family-name']"],
    cardNumber: ["#cardNumber", "input[name='cardNumber']", "input[name='cardnumber']", "input[autocomplete='cc-number']", "input[aria-label*='card number' i]"],
    cardExpiry: ["#cardExpiry", "input[name='cardExpiry']", "input[name='expiry']", "input[autocomplete='cc-exp']", "input[aria-label*='expiration' i]"],
    cardCvv: ["#cardCvv", "#cvv", "#cvc", "input[name='cardCvv']", "input[name='cvv']", "input[name='cvc']", "input[autocomplete='cc-csc']", "input[aria-label*='security' i]"],
    line1: ["#billingLine1", "#billingAddressLine1", "#addressLine1", "input[name='billingLine1']", "input[name='billingAddressLine1']", "input[autocomplete='address-line1']"],
    city: ["#billingCity", "#billingLocality", "#city", "input[name='billingCity']", "input[name='billingLocality']", "input[autocomplete='address-level2']"],
    postalCode: ["#billingPostalCode", "#postalCode", "#zip", "input[name='billingPostalCode']", "input[name='postalCode']", "input[autocomplete='postal-code']"],
    state: ["#billingState", "#billingAdministrativeArea", "#state", "select[name*='state' i]", "select[name*='administrative' i]", "input[name*='state' i]"],
    country: [
      "#country",
      "#billingCountry",
      "#billingCountrySelector",
      "select[name='country']",
      "select[name*='country' i]",
      "select[autocomplete='country']",
      "input[name='country']",
      "input[name*='country' i]",
      "input[autocomplete='country']",
      "[role='combobox'][aria-label*='country' i]",
      "button[id*='country' i]",
      "button[aria-label*='country' i]",
      "[data-testid*='country' i]"
    ]
  };

  const ACTION_WORDS = {
    next: [/continue|next|agree|submit|confirm|pay|subscribe|done|log\s*in|sign\s*in/i, /继续|下一步|同意|提交|确认|支付|购买|订阅|完成|登录/i],
    paypal: [/paypal/i],
    approve: [/agree\s*(?:and)?\s*continue|accept|authorize|approve|continue|pay\s*now/i, /同意|继续|授权|确认|批准/i],
    login: [/login|log\s*in|sign\s*in|continue|next/i, /登录|登入|继续|下一步/i],
    sms: [/send\s*code|resend|text\s*me|sms|continue/i, /发送|重发|短信|验证码|继续/i]
  };

  const PRIMARY_BUTTON_SELECTORS = [
    "button[data-testid='submit-button']",
    "button[data-testid='hosted-payment-submit-button']",
    "button[data-atomic-wait-intent='Submit_Email']",
    "button.SubmitButton--complete",
    "#consentButton",
    "button[name='consentButton']"
  ];

  let running = false;
  let autoRunStarted = false;
  let checkoutWebDirectStarted = false;
  let checkoutWebWatchStarted = false;
  let checkoutWebDirectRunning = false;
  let checkoutWebSubmitted = false;
  let lastOtpFillRequestId = "";
  let otpCodeFetchRunning = false;
  const CHECKOUT_WEB_BILLING_FIELD_IDS = ["email", "phone", "cardNumber", "cardExpiry", "cardCvv", "billingLine1", "billingCity", "billingPostalCode"];

  function log(...args) {
    console.log(LOG_PREFIX, ...args);
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function isTopFrame() {
    try { return window.top === window; } catch (_) { return true; }
  }

  function storageGet(keys) {
    return new Promise((resolve) => chrome.storage.local.get(keys, resolve));
  }

  function storageSet(value) {
    return new Promise((resolve) => chrome.storage.local.set(value, resolve));
  }

  function normalizeProfile(profile = {}) {
    const card = profile.card || {};
    const address = profile.address || {};
    return {
      enabled: profile.enabled !== false,
      poolVersion: profile.poolVersion || "",
      email: profile.email || "",
      phone: profile.phone || "",
      phonePool: Array.isArray(profile.phonePool) ? profile.phonePool.map(normalizePhoneEntry).filter((item) => item.phone || item.otpUrl) : [],
      otpUrl: profile.otpUrl || "",
      password: profile.password || "",
      firstName: profile.firstName || "",
      lastName: profile.lastName || "",
      card: {
        number: card.number || profile.cardNumber || "",
        month: card.month || "",
        year: card.year || "",
        expiry: buildCardExpiry(card) || card.expiry || profile.cardExpiry || "",
        cvv: card.cvv || profile.cardCvv || ""
      },
      cardPool: Array.isArray(profile.cardPool) ? profile.cardPool.map(normalizeCardEntry).filter((item) => item.number) : [],
      address: {
        line1: address.line1 || profile.addressLine1 || "",
        city: address.city || profile.city || "",
        state: address.state || profile.state || "",
        postalCode: address.postalCode || profile.postalCode || "",
        country: address.country || profile.country || "US"
      },
      options: {
        autoSubmit: profile.options?.autoSubmit !== false,
        rotateOnSuccess: profile.options?.rotateOnSuccess !== false,
        useAddressApi: profile.options?.useAddressApi !== false,
        autoRun: profile.options?.autoRun !== false
      }
    };
  }

  function randomEmail() {
    const chars = "abcdefghijklmnopqrstuvwxyz0123456789";
    let local = "";
    for (let i = 0; i < 16; i += 1) local += chars[Math.floor(Math.random() * chars.length)];
    return `${local}@gmail.com`;
  }

  function randomPassword() {
    const lower = "abcdefghijklmnopqrstuvwxyz";
    const upper = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";
    const digits = "0123456789";
    const symbols = "!@#$%^";
    const allChars = lower + upper + digits + symbols;
    let value = lower[Math.floor(Math.random() * lower.length)]
      + upper[Math.floor(Math.random() * upper.length)]
      + digits[Math.floor(Math.random() * digits.length)]
      + symbols[Math.floor(Math.random() * symbols.length)];
    for (let i = value.length; i < 14; i += 1) {
      value += allChars[Math.floor(Math.random() * allChars.length)];
    }
    return value.split("").sort(() => Math.random() - 0.5).join("");
  }

  function normalizePhoneEntry(entry) {
    if (typeof entry === "string") {
      const [phone, otpUrl = ""] = String(entry).split("|").map((part) => part.trim());
      return { phone: phone || "", otpUrl: otpUrl || "" };
    }
    if (!entry || typeof entry !== "object") return { phone: "", otpUrl: "" };
    return {
      phone: entry.phone || entry.number || "",
      otpUrl: entry.otpUrl || entry.url || entry.link || ""
    };
  }

  function normalizeCardEntry(entry) {
    if (typeof entry === "string") {
      const [number = "", month = "", year = "", cvv = ""] = String(entry).split("|").map((part) => part.trim());
      return { number: number.replace(/\D/g, ""), month, year, cvv: cvv.replace(/\D/g, "") };
    }
    if (!entry || typeof entry !== "object") return { number: "", month: "", year: "", cvv: "" };
    return {
      number: String(entry.number || entry.cardNumber || "").replace(/\D/g, ""),
      month: String(entry.month || entry.expiryMonth || entry.expMonth || "").trim(),
      year: String(entry.year || entry.expiryYear || entry.expYear || "").trim(),
      cvv: String(entry.cvv || entry.cardCvv || "").replace(/\D/g, ""),
      expiry: String(entry.expiry || "").trim()
    };
  }

  function buildCardExpiry(card = {}) {
    const month = String(card.month || "").trim();
    const year = String(card.year || "").trim();
    if (month && year) {
      return `${month.padStart(2, "0")} / ${(year.length === 4 ? year.slice(-2) : year).padStart(2, "0")}`;
    }
    const expiry = String(card.expiry || "").trim();
    const match = expiry.match(/(\d{1,2})\D*(\d{2,4})/);
    if (!match) return expiry;
    return `${match[1].padStart(2, "0")} / ${(match[2].length === 4 ? match[2].slice(-2) : match[2]).padStart(2, "0")}`;
  }

  async function readProfile() {
    const data = await storageGet([STORAGE_KEY, STATE_KEY]);
    const rawStored = data[STORAGE_KEY] || {};
    const shouldSeedPools = DEFAULT_PROFILE.poolVersion && rawStored.poolVersion !== DEFAULT_PROFILE.poolVersion;
    const source = shouldSeedPools
      ? {
          ...DEFAULT_PROFILE,
          ...rawStored,
          poolVersion: DEFAULT_PROFILE.poolVersion,
          phone: DEFAULT_PROFILE.phone,
          phonePool: DEFAULT_PROFILE.phonePool,
          otpUrl: DEFAULT_PROFILE.otpUrl,
          card: DEFAULT_PROFILE.card,
          cardPool: DEFAULT_PROFILE.cardPool,
          options: { ...(rawStored.options || {}), ...(DEFAULT_PROFILE.options || {}) }
        }
      : {
          ...DEFAULT_PROFILE,
          ...rawStored,
          options: { ...(DEFAULT_PROFILE.options || {}), ...(rawStored.options || {}) }
        };
    const base = normalizeProfile(source);
    const state = data[STATE_KEY] || {};
    let generatedChanged = false;

    // 每次填表都生成新的邮箱和密码（避免重复使用同一邮箱触发风控）
    if (!base.email) {
      base.email = randomEmail();
      generatedChanged = true;
    }
    if (!base.password) {
      base.password = randomPassword();
      generatedChanged = true;
    }
    if (!base.firstName) base.firstName = "James";
    if (!base.lastName) base.lastName = "Smith";

    const cardPool = base.cardPool.length ? base.cardPool : (base.card.number ? [base.card] : []);
    const phonePool = base.phonePool.length ? base.phonePool : (base.phone ? [normalizePhoneEntry({ phone: base.phone, otpUrl: base.otpUrl })] : []);
    if (cardPool.length) {
      const card = cardPool[Math.abs(Number(state.cardIndex || 0)) % cardPool.length];
      base.card = { ...base.card, ...card, expiry: buildCardExpiry(card) || card.expiry || base.card.expiry || "" };
    }
    if (phonePool.length) {
      const phoneEntry = phonePool[Math.abs(Number(state.phoneIndex || 0)) % phonePool.length];
      base.phone = phoneEntry.phone || base.phone;
      base.otpUrl = phoneEntry.otpUrl || base.otpUrl;
      base.phoneEntry = phoneEntry;
    }
    return { profile: base, state };
  }

  async function updateRuntime(patch) {
    const data = await storageGet([RUNTIME_KEY]);
    const previous = data[RUNTIME_KEY] || {};
    const next = {
      ...previous,
      ...patch,
      updatedAt: Date.now(),
      url: location.href
    };
    await storageSet({ [RUNTIME_KEY]: next });
    updateMiniPanel(next);
    return next;
  }

  function textOf(el) {
    if (!el) return "";
    return [
      el.textContent,
      el.value,
      el.getAttribute?.("aria-label"),
      el.getAttribute?.("title"),
      el.getAttribute?.("placeholder"),
      el.getAttribute?.("name"),
      el.getAttribute?.("data-testid"),
      el.getAttribute?.("data-atomic-wait-intent"),
      el.id
    ].filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
  }

  function escapeHtml(text) {
    return String(text || "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      "\"": "&quot;",
      "'": "&#39;"
    }[ch]));
  }

  function isVisible(el) {
    if (!el || el.disabled || el.readOnly || el.getAttribute?.("aria-disabled") === "true") return false;
    let node = el;
    while (node && node.nodeType === 1) {
      if (node.hidden || node.getAttribute?.("aria-hidden") === "true" || node.getAttribute?.("inert") !== null) return false;
      const style = window.getComputedStyle(node);
      if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse" || Number(style.opacity) === 0) return false;
      node = node.parentElement;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function all(selector) {
    return queryDeepAll(selector).filter(isVisible);
  }

  function candidates(selectors) {
    return selectors.flatMap((selector) => queryDeepAll(selector)).filter(isVisible);
  }

  function queryDeepAll(selector, root = document) {
    const found = [];
    try {
      found.push(...Array.from(root.querySelectorAll(selector)));
    } catch (_) {
      return found;
    }
    const nodes = root.querySelectorAll ? Array.from(root.querySelectorAll("*")) : [];
    for (const node of nodes) {
      if (node.shadowRoot) found.push(...queryDeepAll(selector, node.shadowRoot));
    }
    return found;
  }

  function queryDeepOne(selector) {
    return queryDeepAll(selector)[0] || null;
  }

  function byIdDeep(id) {
    const escaped = window.CSS?.escape ? CSS.escape(id) : String(id).replace(/([^\w-])/g, "\\$1");
    return document.getElementById(id) || queryDeepOne(`#${escaped}`);
  }

  function dispatchValueEvents(el, value = "") {
    const text = String(value ?? "");
    const key = text.slice(-1) || " ";
    try { el.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, composed: true })); } catch (_) {}
    if (typeof InputEvent === "function") {
      try { el.dispatchEvent(new InputEvent("beforeinput", { inputType: "insertText", data: text, bubbles: true, cancelable: true, composed: true })); } catch (_) {}
      try { el.dispatchEvent(new InputEvent("input", { inputType: "insertText", data: text, bubbles: true, composed: true })); } catch (_) {
        el.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
      }
    } else {
      el.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    }
    el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
    el.dispatchEvent(new Event("blur", { bubbles: true, composed: true }));
    try { el.dispatchEvent(new KeyboardEvent("keyup", { key, bubbles: true, cancelable: true, composed: true })); } catch (_) {}
  }

  function setReactValue(el, value) {
    try { el.focus({ preventScroll: true }); } catch (_) {}
    const tracker = el?._valueTracker;
    if (tracker) tracker.setValue("");
    if (!(el instanceof HTMLSelectElement) && tryInsertText(el, value)) return true;
    const proto = el instanceof HTMLSelectElement
      ? HTMLSelectElement.prototype
      : el instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, String(value));
    else el.value = String(value);
    dispatchValueEvents(el, value);
    return true;
  }

  function tryInsertText(el, value) {
    if (typeof document.execCommand !== "function") return false;
    try {
      const next = String(value);
      if (typeof el.select === "function") el.select();
      else if (typeof el.setSelectionRange === "function") el.setSelectionRange(0, String(el.value || "").length);
      const ok = document.execCommand("insertText", false, next);
      if (!ok || !String(el.value || "")) return false;
      el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
      el.dispatchEvent(new Event("blur", { bubbles: true, composed: true }));
      return true;
    } catch (_) {
      return false;
    }
  }

  function nativeSet(el, value) {
    if (!el || value == null || String(value).trim() === "") return false;
    if (el instanceof HTMLSelectElement) return setSelect(el, value);
    return setReactValue(el, value);
  }

  function fillById(id, value) {
    const el = byIdDeep(id);
    if (!el) {
      log("not found", id);
      return false;
    }
    if (el instanceof HTMLSelectElement) return setSelect(el, value);
    return setReactValue(el, value);
  }

  function fillSelectById(id, value) {
    const el = byIdDeep(id);
    if (!el) {
      log("not found", id);
      return false;
    }
    if (el instanceof HTMLSelectElement) return setSelect(el, value);
    return nativeSet(el, value);
  }

  function setSelect(el, wanted) {
    const text = String(wanted || "").toLowerCase();
    const options = Array.from(el.options || []);
    const option = options.find((item) => String(item.value || "").toLowerCase() === text)
      || options.find((item) => {
        const label = `${item.textContent || ""} ${item.value || ""}`.toLowerCase();
        return label.includes(text);
      });
    if (!option) return false;
    setReactValue(el, option.value);
    return true;
  }

  function fillFirst(name, selectors, value) {
    if (!value) return false;
    for (const el of candidates(selectors)) {
      if (nativeSet(el, value)) {
        log("filled", name);
        return true;
      }
    }
    return false;
  }

  function clickByPatterns(patterns, selector = "button, a, [role='button'], input[type='button'], input[type='submit'], [tabindex]") {
    const normalized = Array.isArray(patterns) ? patterns : [patterns];
    const primary = all(PRIMARY_BUTTON_SELECTORS.join(","))
      .find((el) => normalized.some((pattern) => pattern.test(textOf(el))) || /submit|consent/i.test(textOf(el)));
    const target = primary || all(selector).find((el) => normalized.some((pattern) => pattern.test(textOf(el))));
    if (!target) return false;
    target.click();
    return true;
  }

  async function clickByPatternsWithRetry(patterns, retries = 10) {
    for (let i = 0; i <= retries; i += 1) {
      if (clickByPatterns(patterns)) return true;
      if (i < retries) await sleep(BUTTON_RETRY_DELAY_MS);
    }
    return false;
  }

  async function enforceUsCountry() {
    const country = byIdDeep("country") || candidates(SELECTORS.country)[0];
    if (!country) return false;
    const value = String(country.value || "").toUpperCase();
    if (value === "US") return true;
    const changed = setCountryToUs(country);
    if (changed) await sleep(3000);
    return changed;
  }

  function setCountryToUs(country) {
    if (!country) return false;
    if (country instanceof HTMLSelectElement) {
      const options = Array.from(country.options || []);
      const option = options.find((item) => String(item.value || "").toUpperCase() === "US")
        || options.find((item) => /united states|usa|u\.s\.|美国|美國/i.test(`${item.textContent || ""} ${item.value || ""}`));
      return setReactValue(country, option ? option.value : "US");
    }
    if (country instanceof HTMLInputElement || country instanceof HTMLTextAreaElement) {
      return nativeSet(country, "US");
    }
    country.click();
    const option = all("[role='option'], [data-value], li, button, div")
      .find((item) => {
        const text = textOf(item);
        const dataValue = String(item.getAttribute?.("data-value") || item.getAttribute?.("value") || "");
        return /^US$/i.test(dataValue) || /united states|usa|u\.s\.|美国|美國/i.test(text);
      });
    if (!option) return false;
    option.click();
    return true;
  }

  async function enforceUsCountryDeep() {
    const country = findCountryControlDeep();
    if (!country) {
      await updateCountryDebug("country_not_found");
      return false;
    }
    const value = String(country.value || "").toUpperCase();
    if (value === "US") {
      await updateCountryDebug("already_us", country);
      return true;
    }
    const changed = setCountryToUsDeep(country);
    if (changed) await sleep(3000);
    await updateCountryDebug(changed ? "country_changed" : "country_change_failed", country);
    return changed;
  }

  function findCountryControlDeep() {
    return document.getElementById("country")
      || queryDeepOne("#country")
      || candidates(SELECTORS.country)[0]
      || queryDeepAll("select, input, button, [role='combobox']")
        .find((el) => /country|billingcountry|billing country|国家|地區|地区/i.test(textOf(el)));
  }

  function setCountryToUsDeep(country) {
    if (!country) return false;
    if (country instanceof HTMLSelectElement) {
      const options = Array.from(country.options || []);
      const option = options.find((item) => String(item.value || "").toUpperCase() === "US")
        || options.find((item) => /united states|usa|u\.s\.|美国|美國/i.test(`${item.textContent || ""} ${item.value || ""}`));
      if (option) return setReactValue(country, option.value);
      return setReactValue(country, "US");
    }
    if (country instanceof HTMLInputElement || country instanceof HTMLTextAreaElement) {
      country.click();
      nativeSet(country, "United States");
      const option = findCountryOptionDeep();
      if (option) {
        option.click();
        return true;
      }
      return nativeSet(country, "US");
    }
    country.click();
    const option = findCountryOptionDeep();
    if (!option) return false;
    option.click();
    return true;
  }

  function findCountryOptionDeep() {
    return all("[role='option'], [data-value], li, button, div, span")
      .find((item) => {
        const text = textOf(item);
        const dataValue = String(item.getAttribute?.("data-value") || item.getAttribute?.("value") || "");
        return /^US$/i.test(dataValue) || /united states|usa|u\.s\.|美国|美國/i.test(text);
      });
  }

  async function updateCountryDebug(status, control = null) {
    const snapshot = queryDeepAll("select, input, button, [role='combobox']")
      .filter((el) => /country|billingcountry|billing country|国家|地區|地区/i.test(textOf(el)))
      .slice(0, 6)
      .map((el) => ({
        tag: el.tagName,
        id: el.id || "",
        name: el.getAttribute?.("name") || "",
        role: el.getAttribute?.("role") || "",
        value: String(el.value || "").slice(0, 40),
        text: textOf(el).slice(0, 80),
        visible: isVisible(el)
      }));
    await updateRuntime({
      message: `地区切换: ${status}`,
      countryDebug: {
        status,
        frameUrl: location.href,
        control: control ? {
          tag: control.tagName,
          id: control.id || "",
          name: control.getAttribute?.("name") || "",
          role: control.getAttribute?.("role") || "",
          value: String(control.value || "").slice(0, 40),
          text: textOf(control).slice(0, 80)
        } : null,
        candidates: snapshot
      }
    });
  }

  function checkTerms() {
    const include = /agree|agreement|authorize|authorization|consent|policy|terms|automatic|billing|同意|协议|条款|授权|政策|自动续费/i;
    const exclude = /newsletter|marketing|promo|offer|remember|save\s+my|保存信息|记住|营销/i;
    const box = all("input[type='checkbox']").find((item) => !item.checked && include.test(textOf(item.parentElement || item)) && !exclude.test(textOf(item.parentElement || item)));
    if (!box) return false;
    box.click();
    return true;
  }

  function hasCaptcha() {
    return Boolean(
      document.querySelector("iframe[name='recaptcha']") ||
      document.querySelector("#captcha-standalone, .captcha-overlay, .captcha-container") ||
      document.getElementById("captchaHeading") ||
      /captcha|verify you are human|安全验证|人机验证/i.test(document.body?.innerText || "")
    );
  }

  function detectStage() {
    const host = String(location.host || "").toLowerCase();
    const path = String(location.pathname || "");
    const body = String(document.body?.innerText || "").toLowerCase();

    if (/chatgpt\.com/.test(host) && /checkout\/verify|success|subscription|gizmos/.test(path + " " + body)) {
      return STAGES.DONE;
    }
    if (hasCaptcha()) return STAGES.BLOCKED;
    if (isPayPalCheckoutWeb()) {
      if (hasOtpInputs() || /verification|security code|短信|验证码/i.test(body)) return STAGES.PAYPAL_SMS;
      return STAGES.PAYPAL_GUEST;
    }
    if (hasPayPalGuestFields()) {
      if (hasOtpInputs() || /verification|security code|短信|验证码/i.test(body)) return STAGES.PAYPAL_SMS;
      return STAGES.PAYPAL_GUEST;
    }
    if (/pay\.openai\.com|checkout\.stripe\.com|pm-redirects\.stripe\.com/.test(host) || /chatgpt\.com/.test(host) && /checkout/.test(path)) {
      if (findPayPalMethodButton()) return STAGES.OPENAI_CHECKOUT;
      if (hasBillingFields()) return STAGES.OPENAI_BILLING;
      if (findSubmitButton()) return STAGES.OPENAI_SUBMIT;
      return STAGES.OPENAI_CHECKOUT;
    }
    if (/paypal\./.test(host)) {
      if (/checkoutweb/i.test(path) || document.getElementById("cardNumber") || document.getElementById("billingLine1")) {
        if (hasOtpInputs() || /verification|security code|短信|验证码/.test(body)) return STAGES.PAYPAL_SMS;
        return STAGES.PAYPAL_GUEST;
      }
      if (/webapps\/hermes|agreements|billing|review/.test(path) || /agree|authorize|automatic payments|review|同意|授权/.test(body)) {
        return STAGES.PAYPAL_APPROVE;
      }
      if (findPasswordField()) return STAGES.PAYPAL_LOGIN_PASSWORD;
      if (findEmailField()) return STAGES.PAYPAL_LOGIN_EMAIL;
      return STAGES.PAYPAL_REVIEW;
    }
    return STAGES.UNKNOWN;
  }

  function findPayPalMethodButton() {
    return all("[data-testid='paypal-accordion-item-button'], .paypal-accordion-item button, button, [role='button'], label")
      .find((el) => /paypal/i.test(textOf(el)));
  }

  function hasBillingFields() {
    return Boolean(
      candidates(SELECTORS.line1).length ||
      candidates(SELECTORS.city).length ||
      candidates(SELECTORS.postalCode).length ||
      candidates(SELECTORS.country).length
    );
  }

  function hasPayPalGuestFields() {
    return Boolean(
      candidates(SELECTORS.cardNumber).length ||
      candidates(SELECTORS.cardExpiry).length ||
      candidates(SELECTORS.cardCvv).length ||
      queryDeepOne("#cardNumber") ||
      queryDeepOne("#billingLine1") ||
      queryDeepOne("#country")
    );
  }

  function findSubmitButton() {
    return all("button[type='submit'], input[type='submit'], button, [role='button']")
      .find((el) => /subscribe|continue|confirm|pay|start\s*subscription|place\s*order|agree|同意|继续|确认|支付|订阅/i.test(textOf(el)));
  }

  function findEmailField() {
    return candidates(SELECTORS.email)[0] || all("input").find((input) => {
      const type = String(input.type || "").toLowerCase();
      return type !== "password" && /email|login|user|邮箱|账号/i.test(textOf(input));
    });
  }

  function findPasswordField() {
    return candidates(SELECTORS.password)[0] || all("input[type='password']")[0] || null;
  }

  function hasOtpInputs() {
    return findOtpInputs().length > 0;
  }

  function findOtpInputs() {
    const allInputs = queryDeepAll("input")
      .filter((el) => !el.closest("#gpt-paypal-autofill-panel") && isVisible(el));

    const hosted = Array.from({ length: 6 }, (_, index) => document.getElementById(`ci-ciBasic-${index}`))
      .filter((el) => el && isVisible(el));
    if (hosted.length >= 6) return hosted;

    function hints(el) {
      const parts = [
        el.id, el.name,
        el.getAttribute("aria-label"), el.getAttribute("placeholder"),
        el.getAttribute("autocomplete"), el.getAttribute("data-testid"),
        el.getAttribute("inputmode"), el.getAttribute("pattern")
      ];
      try { if (el.labels?.length) parts.push(el.labels[0].textContent); } catch (_) {}
      let parent = el.parentElement;
      for (let i = 0; i < 3 && parent; i++) { parts.push(parent.textContent); parent = parent.parentElement; }
      return parts.filter(Boolean).join(" ").replace(/\s+/g, " ");
    }

    const isOtpLabel = (text) => /(?:otp|one[-\s]?time|one.?time.?pass|verification|verify|confirm|confirmation|security.?code|passcode|authentication|challenge|sms.?code|text.?code|login.?code|code|验证码|验证|安全码|认证码|短信码)/i.test(String(text || ""));
    const isOtpContext = (text) => /enter.{0,40}code|6[-\s]?digit|six[-\s]?digit|security code|verification|verify|confirm.{0,40}(?:phone|mobile|number)|code sent|we sent|text code|sent a \d[-\s]?digit code|验证码|验证|短信|安全码/i.test(String(text || ""));
    const isDefinitelyBilling = (text) => /zip|postal|postcode|billing|address|city|state|province|country|card|cvv|cvc|expiry|expiration|email|password|first.?name|last.?name|full.?name|phone|mobile|tel|telephone|phone.?number/i.test(String(text || ""));
    const isPhoneNumberEntry = (el, text = hints(el)) => {
      if (el.autocomplete === "one-time-code" || isOtpLabel(text)) return false;
      const type = String(el.type || "").toLowerCase();
      const autocomplete = String(el.getAttribute("autocomplete") || "").toLowerCase();
      const name = String(el.name || el.id || "").toLowerCase();
      return type === "tel" ||
        autocomplete === "tel" ||
        /(?:^|[-_\s])(?:phone|mobile|tel|telephone)(?:$|[-_\s])/i.test(name) ||
        /(?:phone|mobile|tel|telephone|phone\s*number|mobile\s*number|手机号|手机|電話|电话号码)/i.test(String(text || ""));
    };
    const isPotentialCodeInput = (el) => {
      const type = String(el.type || "text").toLowerCase();
      if (!["", "text", "tel", "number", "password"].includes(type)) return false;
      const max = Number(el.getAttribute("maxlength") || el.maxLength || 0);
      const mode = String(el.getAttribute("inputmode") || "").toLowerCase();
      const pattern = String(el.getAttribute("pattern") || "");
      return max === 1 ||
        (max >= 4 && max <= 8) ||
        /numeric|decimal|tel/.test(mode) ||
        /\\d|\[0-9\]/.test(pattern) ||
        /code|otp|pin|verification|challenge|auth/i.test(hints(el));
    };

    function pageContext() {
      const modal = document.querySelector('[role="dialog"], [aria-modal="true"], .modal, [class*="modal"]');
      if (modal) return modal.textContent || "";
      const els = document.querySelectorAll("main, form, section, article, div, p, span, h1, h2, h3, label, button");
      let text = "";
      for (let i = 0; i < Math.min(els.length, 200); i++) text += " " + (els[i].textContent || "");
      return text;
    }
    const ctxText = pageContext();
    const hasOtpCtx = isOtpContext(ctxText);

    const singleOtp = allInputs.filter((el) => {
      const h = hints(el);
      const max = Number(el.getAttribute("maxlength") || el.maxLength || 0);
      return el.autocomplete === "one-time-code" ||
        isOtpLabel(h) ||
        (hasOtpCtx && !isPhoneNumberEntry(el, h) && max >= 4 && max <= 8 && isPotentialCodeInput(el)) ||
        (hasOtpCtx && !isPhoneNumberEntry(el, h) && isPotentialCodeInput(el) && !isDefinitelyBilling(h));
    });
    if (singleOtp.length === 1) return singleOtp;
    const labeledSingles = singleOtp.filter((el) => isOtpLabel(hints(el)) || el.autocomplete === "one-time-code");
    if (labeledSingles.length === 1) return labeledSingles;

    const multiLabel = allInputs.filter((el) => {
      const max = Number(el.getAttribute("maxlength") || el.maxLength || 0);
      return max === 1 && isOtpLabel(hints(el));
    });
    if (multiLabel.length >= 4) return orderByPosition(multiLabel).slice(0, 8);

    const multiCtx = allInputs.filter((el) => {
      const max = Number(el.getAttribute("maxlength") || el.maxLength || 0);
      const type = String(el.type || "").toLowerCase();
      const mode = String(el.getAttribute("inputmode") || "").toLowerCase();
      const h = hints(el);
      if (isDefinitelyBilling(h) || isPhoneNumberEntry(el, h)) return false;
      return (max === 1 || mode === "numeric") && ["", "text", "tel", "number", "password"].includes(type);
    });
    if (multiCtx.length >= 4 && hasOtpCtx) return orderByPosition(multiCtx).slice(0, 8);

    const compact = allInputs.filter((el) => {
      const max = Number(el.getAttribute("maxlength") || el.maxLength || 0);
      const mode = String(el.getAttribute("inputmode") || "").toLowerCase();
      const h = hints(el);
      if (isDefinitelyBilling(h) || isPhoneNumberEntry(el, h)) return false;
      if (max > 1 || !/numeric|decimal|tel/.test(mode)) return false;
      const rect = el.getBoundingClientRect();
      return rect.width >= 24 && rect.width <= 96 && rect.height >= 28 && rect.height <= 96;
    });
    if (compact.length >= 4 && hasOtpCtx) return orderByPosition(compact).slice(0, 8);

    const contextualSingles = allInputs.filter((el) => {
      const max = Number(el.getAttribute("maxlength") || el.maxLength || 0);
      const h = hints(el);
      if (isDefinitelyBilling(h) || isPhoneNumberEntry(el, h)) return false;
      return hasOtpCtx && (max === 0 || max >= 4 && max <= 8) && isPotentialCodeInput(el);
    });
    if (contextualSingles.length === 1) return contextualSingles;

    return [];
  }

  function orderByPosition(inputs) {
    return inputs.slice().sort((a, b) => {
      const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
      return ra.top - rb.top || ra.left - rb.left;
    });
  }

  function otpInputDebugSnapshot(limit = 8) {
    return queryDeepAll("input").filter(isVisible).slice(0, limit).map((el) => {
      const rect = el.getBoundingClientRect();
      return {
        id: el.id || "",
        name: el.getAttribute("name") || "",
        type: el.type || "",
        autocomplete: el.getAttribute("autocomplete") || "",
        inputmode: el.getAttribute("inputmode") || "",
        maxlength: el.getAttribute("maxlength") || "",
        placeholder: el.getAttribute("placeholder") || "",
        label: (el.getAttribute("aria-label") || "").slice(0, 40),
        size: `${Math.round(rect.width)}x${Math.round(rect.height)}`
      };
    });
  }

  async function fetchUsAddress() {
    try {
      const response = await chrome.runtime.sendMessage({ type: "FETCH_US_ADDRESS" });
      return response?.ok ? response.address || {} : {};
    } catch (_) {
      return {};
    }
  }

  async function resolveAddress(profile) {
    const address = profile.address || {};
    const needsAddress = !address.line1 || !address.city || !address.state || !address.postalCode;
    const fetched = needsAddress && profile.options.useAddressApi ? await fetchUsAddress() : {};
    return {
      line1: address.line1 || fetched.line1 || "123 Main St",
      city: address.city || fetched.city || "New York",
      state: address.state || fetched.state || "New York",
      postalCode: address.postalCode || fetched.postalCode || "10001",
      country: address.country || fetched.country || "US"
    };
  }

  function formatPhone(phone) {
    const raw = String(phone || "").trim();
    const digits = raw.replace(/\D/g, "");
    if (digits.length === 11 && digits.startsWith("1")) return digits.slice(1);
    if (digits.length > 10) return digits.slice(-10);
    if (digits.length === 10) return digits;
    return raw;
  }

  function checkoutExpiryParts(expiry) {
    const match = String(expiry || "").match(/(\d{1,2})\D*(\d{2,4})/);
    if (!match) return null;
    const month = match[1].padStart(2, "0");
    const yy = (match[2].length === 4 ? match[2].slice(-2) : match[2]).padStart(2, "0");
    const yyyy = match[2].length === 4 ? match[2] : `20${yy}`;
    return { month, yy, yyyy };
  }

  function uniqueCandidates(values) {
    const seen = new Set();
    return values
      .map((value) => String(value || "").trim())
      .filter((value) => value && !seen.has(value) && seen.add(value));
  }

  function checkoutPhoneCandidates(phone) {
    const raw = String(phone || "").trim();
    const digits = raw.replace(/\D/g, "");
    const national = formatPhone(raw);
    return uniqueCandidates([
      national,
      digits.length === 10 ? `1${digits}` : "",
      digits.length === 10 ? `+1${digits}` : "",
      raw
    ]);
  }

  function checkoutExpiryCandidates(expiry) {
    const raw = String(expiry || "").trim();
    const parts = checkoutExpiryParts(raw);
    if (!parts) return uniqueCandidates([raw]);
    return uniqueCandidates([
      `${parts.month} / ${parts.yy}`,
      `${parts.month}/${parts.yy}`,
      `${parts.month}${parts.yy}`,
      `${parts.month} / ${parts.yyyy}`,
      `${parts.month}/${parts.yyyy}`,
      raw
    ]);
  }

  function isPayPalCheckoutWeb() {
    return /paypal\./i.test(location.host || "") && /\/checkoutweb\//i.test(location.pathname || "");
  }

  function normalizePayPalCheckoutUrl() {
    if (!isPayPalCheckoutWeb()) return false;
    const url = new URL(location.href);
    const country = String(url.searchParams.get("country.x") || "").toUpperCase();
    const locale = String(url.searchParams.get("locale.x") || "");
    if (country && country !== "US" || locale && !/^en_US$/i.test(locale)) {
      updateRuntime({ message: "地区参数不重刷 URL；仅在表单内切换 country=US" }).catch(() => {});
    }
    return false;
  }

  async function executeOpenAiCheckout(profile) {
    const payPalButton = findPayPalMethodButton();
    if (payPalButton) {
      payPalButton.click();
      await sleep(500);
      payPalButton.click();
    }
    await sleep(2500);
    await enforceUsCountryDeep();
    const address = await resolveAddress(profile);
    let filled = 0;
    filled += fillFirst("address.line1", SELECTORS.line1, address.line1) ? 1 : 0;
    filled += fillFirst("address.city", SELECTORS.city, address.city) ? 1 : 0;
    filled += fillFirst("address.postalCode", SELECTORS.postalCode, address.postalCode) ? 1 : 0;
    filled += fillFirst("address.state", SELECTORS.state, address.state) ? 1 : 0;
    filled += fillFirst("address.country", SELECTORS.country, address.country) ? 1 : 0;
    checkTerms();
    const submitted = profile.options.autoSubmit && await clickByPatternsWithRetry(ACTION_WORDS.next, 10);
    return { ok: true, action: submitted ? "submitted" : "filled", message: submitted ? "OpenAI/Stripe 已提交" : `OpenAI/Stripe 已填 ${filled} 项` };
  }

  async function executePayPalLoginEmail(profile) {
    if (!profile.email) return { ok: false, action: "waiting", message: "未配置 PayPal 邮箱" };
    const input = findEmailField();
    const filled = input && nativeSet(input, profile.email);
    const submitted = profile.options.autoSubmit && await clickByPatternsWithRetry(ACTION_WORDS.login, 10);
    return { ok: Boolean(filled || submitted), action: submitted ? "submitted" : "filled", message: submitted ? "PayPal 邮箱已提交" : "PayPal 邮箱已填" };
  }

  async function executePayPalLoginPassword(profile) {
    if (!profile.password) return { ok: false, action: "waiting", message: "未配置 PayPal 密码" };
    const input = findPasswordField();
    const filled = input && nativeSet(input, profile.password);
    const submitted = profile.options.autoSubmit && await clickByPatternsWithRetry(ACTION_WORDS.login, 10);
    return { ok: Boolean(filled || submitted), action: submitted ? "submitted" : "filled", message: submitted ? "PayPal 密码已提交" : "PayPal 密码已填" };
  }

  async function executePayPalGuest(profile) {
    if (isPayPalCheckoutWeb()) return executePayPalCheckoutWeb(profile);
    await enforceUsCountryDeep();
    const address = await resolveAddress(profile);
    let filled = 0;
    filled += fillFirst("email", SELECTORS.email, profile.email) ? 1 : 0;
    filled += fillFirst("phone", SELECTORS.phone, formatPhone(profile.phone)) ? 1 : 0;
    filled += fillFirst("password", SELECTORS.password, profile.password) ? 1 : 0;
    filled += fillFirst("firstName", SELECTORS.firstName, profile.firstName) ? 1 : 0;
    filled += fillFirst("lastName", SELECTORS.lastName, profile.lastName) ? 1 : 0;
    filled += fillFirst("cardNumber", SELECTORS.cardNumber, profile.card.number) ? 1 : 0;
    filled += fillFirst("cardExpiry", SELECTORS.cardExpiry, profile.card.expiry) ? 1 : 0;
    filled += fillFirst("cardCvv", SELECTORS.cardCvv, profile.card.cvv) ? 1 : 0;
    filled += fillFirst("address.line1", SELECTORS.line1, address.line1) ? 1 : 0;
    filled += fillFirst("address.city", SELECTORS.city, address.city) ? 1 : 0;
    filled += fillFirst("address.postalCode", SELECTORS.postalCode, address.postalCode) ? 1 : 0;
    filled += fillFirst("address.state", SELECTORS.state, address.state) ? 1 : 0;
    filled += fillFirst("address.country", SELECTORS.country, address.country) ? 1 : 0;
    checkTerms();
    const submitted = profile.options.autoSubmit && await clickByPatternsWithRetry(ACTION_WORDS.next, 10);
    return { ok: filled > 0 || submitted, action: submitted ? "submitted" : "filled", message: submitted ? "PayPal 填表已提交" : `PayPal 已填 ${filled} 项` };
  }

  function buildCheckoutWebPayload(profile, address) {
    const phoneCandidates = checkoutPhoneCandidates(profile.phone);
    const cardExpiryCandidates = checkoutExpiryCandidates(profile.card.expiry);
    return {
      autoSubmit: profile.options.autoSubmit !== false,
      email: profile.email,
      phone: phoneCandidates[0] || formatPhone(profile.phone),
      phoneCandidates,
      cardNumber: profile.card.number,
      cardExpiry: cardExpiryCandidates[0] || profile.card.expiry,
      cardExpiryCandidates,
      cardCvv: profile.card.cvv,
      password: profile.password,
      firstName: profile.firstName,
      lastName: profile.lastName,
      address: {
        line1: address.line1,
        city: address.city,
        state: address.state,
        postalCode: address.postalCode,
        country: address.country || "US"
      }
    };
  }

  async function fillCheckoutWebInMainWorld(profile, address) {
    try {
      const response = await chrome.runtime.sendMessage({
        type: "PAYPAL_AUTOFILL_MAIN_WORLD_CHECKOUTWEB",
        payload: buildCheckoutWebPayload(profile, address)
      });
      if (response?.ok && response.result?.ok) return response.result;
      return { ok: false, error: response?.error || "main world fill failed" };
    } catch (error) {
      return { ok: false, error: error?.message || String(error) };
    }
  }

  async function fillCheckoutWebWithDebugger(profile, address) {
    try {
      const response = await chrome.runtime.sendMessage({
        type: "PAYPAL_AUTOFILL_DEBUGGER_CHECKOUTWEB",
        payload: buildCheckoutWebPayload(profile, address)
      });
      if (response?.ok && response.result?.ok) return response.result;
      return { ok: false, error: response?.error || response?.result?.error || "debugger fill failed" };
    } catch (error) {
      return { ok: false, error: error?.message || String(error) };
    }
  }

  function checkoutWebMainResultToAction(result) {
    if (!result?.ok) return null;
    if (result.submitted) checkoutWebSubmitted = true;
    const missing = Array.isArray(result.missing) ? result.missing : [];
    if (!result.ready) {
      return {
        ok: false,
        action: "waiting",
        message: `PayPal checkoutweb 等待订单表字段: ${missing.join(", ") || "unknown"}`,
        filled: Number(result.filled || 0),
        ready: false,
        missing
      };
    }
    return {
      ok: true,
      action: result.submitted ? "submitted" : "filled",
      message: result.submitted ? "PayPal checkoutweb 已提交" : `PayPal checkoutweb 主环境已填 ${Number(result.filled || 0)} 项`,
      filled: Number(result.filled || 0),
      ready: true,
      missing
    };
  }

  async function executePayPalCheckoutWeb(profile) {
    await sleep(2000);
    const address = await resolveAddress(profile);
    const fillResult = await fillCheckoutWebFormWhenReady(profile, address);
    const filled = fillResult.filled;
    if (!fillResult.ready) {
      await updateRuntime({ message: `PayPal checkoutweb 等待订单表字段: ${fillResult.missing.join(", ")}` });
      return { ok: false, action: "waiting", message: "PayPal checkoutweb 订单表字段未出现，未点击提交" };
    }
    checkTerms();
    await sleep(500);
    const submitted = profile.options.autoSubmit && await clickCheckoutWebButtonWithRetry(10);
    if (submitted) checkoutWebSubmitted = true;
    return { ok: filled > 0 || submitted, action: submitted ? "submitted" : "filled", message: submitted ? "PayPal checkoutweb 已提交" : `PayPal checkoutweb 已填 ${filled} 项` };
  }

  async function clickCheckoutWebButtonWithRetry(retries = 10) {
    for (let i = 0; i <= retries; i += 1) {
      const clicked = clickCheckoutWebButton();
      if (clicked) return true;
      if (i < retries) await sleep(1000);
    }
    return false;
  }

  function dispatchRobustClick(el) {
    if (!el) return false;
    try { el.scrollIntoView({ block: "center", inline: "center" }); } catch (_) {}
    try { el.focus({ preventScroll: true }); } catch (_) {}
    for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
      try {
        const event = type.startsWith("pointer") && typeof PointerEvent === "function"
          ? new PointerEvent(type, { bubbles: true, cancelable: true, pointerType: "mouse", isPrimary: true })
          : new MouseEvent(type, { bubbles: true, cancelable: true, view: window });
        el.dispatchEvent(event);
      } catch (_) {}
    }
    try { el.click(); } catch (_) {}
    return true;
  }

  function requestCheckoutWebFormSubmit() {
    const btn = document.querySelector('button[data-testid="submit-button"]')
      || document.querySelector('button[data-testid="hosted-payment-submit-button"]')
      || document.querySelector('button[data-atomic-wait-intent="Submit_Email"]')
      || document.querySelector("button.SubmitButton--complete")
      || Array.from(document.querySelectorAll("button, input[type='submit'], [role='button']")).find((item) => /agree|create\s*account|sign\s*up|submit|continue|confirm|注册|同意|创建|继续|确认/i.test(String(item.textContent || item.value || item.getAttribute?.("aria-label") || "")));
    const form = btn?.form || btn?.closest?.("form") || document.querySelector("form");
    if (!form) return false;
    try {
      if (btn) form.requestSubmit(btn);
      else form.requestSubmit();
      return true;
    } catch (_) {
      try {
        form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
        return true;
      } catch (_) {
        return false;
      }
    }
  }

  function clickCheckoutWebButton() {
    let btn = document.querySelector('button[data-testid="submit-button"]')
      || document.querySelector('button[data-testid="hosted-payment-submit-button"]')
      || document.querySelector('button[data-atomic-wait-intent="Submit_Email"]')
      || document.querySelector("button.SubmitButton--complete");
    if (!btn) {
      const buttons = Array.from(document.querySelectorAll("button"));
      btn = buttons.find((item) => {
        const text = String(item.textContent || "").trim();
        return ["下一页", "Next", "Subscribe", "Pay", "Continue", "Agree", "Confirm", "Done"].includes(text);
      });
      // 也匹配包含 "Agree" 的按钮（如 "Agree & Create Account"）
      if (!btn) {
        btn = buttons.find((item) => /agree|create\s*account|sign\s*up|注册|同意|创建/i.test(String(item.textContent || "")));
      }
    }
    if (!btn) {
      log("no submit button, all buttons:", Array.from(document.querySelectorAll("button")).map(b => b.textContent.trim().substring(0, 40)).join(" | "));
      return false;
    }
    if (btn.disabled) { log("button disabled:", btn.textContent.trim()); return false; }
    const rect = btn.getBoundingClientRect();
    if (rect.height === 0) { log("button not visible:", btn.textContent.trim()); return false; }
    log("clicking:", btn.textContent.trim(), "type:", btn.type, "testid:", btn.getAttribute("data-testid"));
    dispatchRobustClick(btn);
    return true;
  }

  function checkoutWebBillingFormStillVisible() {
    const signal = checkoutWebBillingFormSignal();
    return signal.visibleCount >= 4 || (signal.submitVisible && signal.visibleCount >= 2);
  }

  function checkoutWebBillingFormSignal() {
    const fields = CHECKOUT_WEB_BILLING_FIELD_IDS.map((id) => {
      const el = byIdDeep(id);
      const visible = Boolean(el && isVisible(el));
      const value = el && "value" in el ? String(el.value || "") : "";
      return {
        id,
        present: Boolean(el),
        visible,
        length: value.length
      };
    });
    const submitButton = document.querySelector('button[data-testid="submit-button"]')
      || document.querySelector('button[data-testid="hosted-payment-submit-button"]')
      || document.querySelector('button[data-atomic-wait-intent="Submit_Email"]')
      || document.querySelector("button.SubmitButton--complete");
    const visibleCount = fields.filter((field) => field.visible).length;
    return {
      fields,
      presentCount: fields.filter((field) => field.present).length,
      visibleCount,
      filledVisibleCount: fields.filter((field) => field.visible && field.length > 0).length,
      submitVisible: Boolean(submitButton && isVisible(submitButton))
    };
  }

  function checkoutWebSubmitDiagnostics() {
    const fields = CHECKOUT_WEB_BILLING_FIELD_IDS.map((id) => {
      const el = byIdDeep(id);
      return {
        id,
        present: Boolean(el),
        visible: Boolean(el && isVisible(el)),
        length: el && "value" in el ? String(el.value || "").length : 0,
        valid: el && typeof el.checkValidity === "function" ? el.checkValidity() : true,
        invalid: el?.getAttribute?.("aria-invalid") || "",
        validation: el?.validationMessage || "",
        pattern: el?.getAttribute?.("pattern") || "",
        maxlength: el?.getAttribute?.("maxlength") || ""
      };
    });
    const alerts = Array.from(document.querySelectorAll("[role='alert'], .error, [class*='error' i], [data-testid*='error' i], [aria-live]"))
      .filter(isVisible)
      .map((el) => String(el.textContent || "").replace(/\s+/g, " ").trim())
      .filter(Boolean)
      .slice(0, 6);
    const buttons = Array.from(document.querySelectorAll("button"))
      .filter(isVisible)
      .map((button) => ({
        text: String(button.textContent || "").replace(/\s+/g, " ").trim().slice(0, 80),
        disabled: Boolean(button.disabled),
        ariaDisabled: button.getAttribute("aria-disabled") || "",
        testid: button.getAttribute("data-testid") || ""
      }))
      .slice(0, 8);
    return { fields, alerts, buttons };
  }

  async function waitForCheckoutWebProgress(submittedUrl, waitMs = 5000) {
    const endAt = Date.now() + waitMs;
    while (Date.now() < endAt) {
      if (location.href !== submittedUrl) return { progressed: true, reason: "url_changed" };
      if (findOtpInputs().length > 0) return { progressed: true, reason: "otp_visible" };
      await sleep(500);
    }
    return checkoutWebBillingFormStillVisible()
      ? { progressed: false, reason: "still_on_billing_form" }
      : { progressed: false, reason: "checkout_pending_no_otp" };
  }

  async function runCheckoutWebUserscriptFlow(options = {}) {
    if (!isTopFrame()) return false;
    if (checkoutWebDirectRunning || !isPayPalCheckoutWeb()) return false;
    if (checkoutWebDirectStarted && !options.force) return false;
    checkoutWebDirectStarted = true;
    checkoutWebDirectRunning = true;
    try {
      const { profile } = await readProfile();
      if (!profile.enabled) return true;
      if (hasOtpInputs()) {
        log("PayPal OTP is visible; skipped checkout autofill");
        const smsResult = await executePayPalSms(profile);
        await updateRuntime({
          status: smsResult.ok ? "running" : "waiting",
          stage: STAGES.PAYPAL_SMS,
          stageLabel: STAGE_LABELS[STAGES.PAYPAL_SMS],
          message: smsResult.message || "PayPal OTP is visible; skipped checkout autofill"
        });
        return true;
      }

      await updateRuntime({ status: "running", stage: STAGES.PAYPAL_GUEST, stageLabel: STAGE_LABELS[STAGES.PAYPAL_GUEST], message: "PayPal checkoutweb 油猴式流程启动" });
      await sleep(2000);

      const address = await resolveAddress(profile);
      const fillResult = await fillCheckoutWebFormWhenReady(profile, address);
      const filled = fillResult.filled;
      log("fill done, ready=" + fillResult.ready + ", filled=" + filled + ", missing=" + (fillResult.missing || []).join(","));
      let mainSubmitResult = await fillCheckoutWebWithDebugger(profile, address);
      log("debugger checkout result:", JSON.stringify({
        ok: Boolean(mainSubmitResult?.ok),
        ready: Boolean(mainSubmitResult?.ready),
        submitted: Boolean(mainSubmitResult?.submitted),
        filled: Number(mainSubmitResult?.filled || 0),
        missing: mainSubmitResult?.missing || [],
        invalid: mainSubmitResult?.invalid || [],
        error: mainSubmitResult?.error || ""
      }));
      if (!mainSubmitResult?.ok) {
        mainSubmitResult = await fillCheckoutWebInMainWorld(profile, address);
        log("main-world checkout result:", JSON.stringify({
          ok: Boolean(mainSubmitResult?.ok),
          ready: Boolean(mainSubmitResult?.ready),
          submitted: Boolean(mainSubmitResult?.submitted),
          filled: Number(mainSubmitResult?.filled || 0),
          missing: mainSubmitResult?.missing || []
        }));
      }

      // 不管 ready 状态，直接尝试提交（PayPal 可能重新格式化值导致验证失败）
      if (mainSubmitResult?.source === "debugger" && !mainSubmitResult.ready) {
        log("debugger fill did not stick; stop before submit:", JSON.stringify({ missing: mainSubmitResult.missing || [] }));
        await updateRuntime({ status: "waiting", stage: STAGES.PAYPAL_GUEST, stageLabel: STAGE_LABELS[STAGES.PAYPAL_GUEST], message: "PayPal fields did not persist after debugger input; stopped before retry loop" });
        return true;
      }

      checkTerms();
      await sleep(500);
      const submitted = Boolean(mainSubmitResult?.submitted)
        || (mainSubmitResult?.source !== "debugger" && await clickCheckoutWebButtonWithRetry(10));
      if (submitted) checkoutWebSubmitted = true;
      log("submit result: " + submitted);
      await updateRuntime({
        status: submitted ? "running" : "waiting",
        stage: STAGES.PAYPAL_GUEST,
        stageLabel: STAGE_LABELS[STAGES.PAYPAL_GUEST],
        message: submitted ? "PayPal checkoutweb 已点击提交" : `PayPal checkoutweb 已填 ${filled} 项，未找到提交按钮`
      });

      if (!submitted) return true;

      // 等待页面响应提交（表单提交可能触发页面重载）
      const submittedUrl = location.href;
      log("submitted at:", submittedUrl);
      await sleep(3000);
      let submitProgress = await waitForCheckoutWebProgress(submittedUrl, 1000);
      log("checkout submit progress:", submitProgress.reason);
      if (!submitProgress.progressed && checkoutWebBillingFormStillVisible()) {
        log("checkout submit diagnostics:", JSON.stringify(checkoutWebSubmitDiagnostics()));
        await updateRuntime({ status: "waiting", stage: STAGES.PAYPAL_GUEST, stageLabel: STAGE_LABELS[STAGES.PAYPAL_GUEST], message: "PayPal submit did not advance; stopped without refill retry" });
        return true;
      }

      // 如果页面没有变化（URL 相同、表单字段仍为空），尝试直接提交表单
      if (location.href === submittedUrl) {
        const emailEl = document.getElementById("email");
        const cardEl = document.getElementById("cardNumber");
        if (emailEl && cardEl && !emailEl.value && !cardEl.value) {
          log("button click did not advance and fields are empty; stopped without refill retry");
          await updateRuntime({ status: "waiting", stage: STAGES.PAYPAL_GUEST, stageLabel: STAGE_LABELS[STAGES.PAYPAL_GUEST], message: "PayPal submit cleared fields; stopped without refill retry" });
          return true;
        }
      }

      // 检测页面是否跳转（URL 变化 = 页面重载或导航）
      if (location.href !== submittedUrl) {
        log("page navigated after submit to:", location.href, "- new page script will handle");
        return true;
      }

      // 检测风控/错误信息
      const bodyText = (document.body?.innerText || "").substring(0, 800);
      if (/unable to complete|try a different|unable to process|declined|couldn't\s*process|can't\s*complete|not\s*able\s*to|失败|无法完成|换一个|无法处理/i.test(bodyText)) {
        log("detected error/risk control after submit, auto-rotating pools");
        await advancePools({ card: true, phone: true });
        const rotated = await readProfile();
        log("rotated to phone:", rotated.profile.phone, "card:", rotated.profile.card.number);
        await updateRuntime({ status: "waiting", message: `风控/错误：已自动轮换手机号和卡号，请重新点击支付链接重试` });
        return true;
      }

      // 检测表单字段是否为空（页面可能静默重载，提交失败）
      const emailEl = document.getElementById("email");
      const cardEl = document.getElementById("cardNumber");
      if (emailEl && cardEl && !emailEl.value && !cardEl.value) {
        log("form fields empty after submit; stopped without refill retry");
        await updateRuntime({ status: "waiting", stage: STAGES.PAYPAL_GUEST, stageLabel: STAGE_LABELS[STAGES.PAYPAL_GUEST], message: "PayPal fields are empty after submit; stopped without refill retry" });
        return true;
      }

      // 检测是否已有 OTP 输入框（页面已进入短信验证阶段）
      const otpInputs = findOtpInputs();
      if (otpInputs.length > 0) {
        log("OTP inputs found immediately after submit:", otpInputs.length);
        const smsResult = await executePayPalSms(profile);
        log("OTP result: " + smsResult.message);
        await updateRuntime({ status: smsResult.ok ? "running" : "waiting", stage: STAGES.PAYPAL_SMS, stageLabel: STAGE_LABELS[STAGES.PAYPAL_SMS], message: smsResult.message });
        return true;
      }

      // 进入 OTP 轮询等待短信验证页面
      await updateRuntime({ stage: STAGES.PAYPAL_SMS, stageLabel: STAGE_LABELS[STAGES.PAYPAL_SMS], message: "等待短信验证页面..." });
      if (checkoutWebBillingFormStillVisible()) {
        log("checkout submit remained on billing form; not starting OTP poll:", JSON.stringify(checkoutWebBillingFormSignal()));
        await updateRuntime({ status: "waiting", stage: STAGES.PAYPAL_GUEST, stageLabel: STAGE_LABELS[STAGES.PAYPAL_GUEST], message: "PayPal billing form is still visible; not polling OTP yet" });
        return true;
      }

      beginOtpCodeFetch(profile, "checkoutweb-submit");
      let sendCodeClicked = false;
      for (let i = 0; i < 30; i += 1) {
        await sleep(2000);
        if (i > 0 && i % 10 === 0) beginOtpCodeFetch(profile, "checkoutweb-wait-" + i);

        // 检测页面是否跳转
        if (location.href !== submittedUrl) {
          log("page navigated to:", location.href, "- stopping poll, new page script will handle OTP");
          return true;
        }

        const currentOtpInputs = findOtpInputs();
        const allInputs = queryDeepAll("input").filter(isVisible);
        log("OTP check " + i + ": found " + currentOtpInputs.length + "/" + allInputs.length + " visible inputs, url=" + location.pathname);
        if (!currentOtpInputs.length && i % 3 === 0) {
          log("OTP visible input snapshot:", JSON.stringify(otpInputDebugSnapshot()));
        }

        // 检测风控/错误信息
        const currentBodyText = (document.body?.innerText || "").substring(0, 500);
        if (/unable to complete|try a different|unable to process|declined|couldn't\s*process|can't\s*complete|not\s*able\s*to|失败|无法完成|换一个|无法处理/i.test(currentBodyText)) {
          log("detected error/risk control during OTP poll, auto-rotating pools");
          await advancePools({ card: true, phone: true });
          const rotated = await readProfile();
          log("rotated to phone:", rotated.profile.phone, "card:", rotated.profile.card.number);
          await updateRuntime({ status: "waiting", message: `风控/错误：已自动轮换手机号和卡号，请重新点击支付链接重试` });
          return true;
        }

        // 如果还没点过"发送验证码"按钮，先找一下
        if (!sendCodeClicked) {
          const sendBtn = Array.from(document.querySelectorAll("button, a, [role='button']")).find((el) => {
            const text = String(el.textContent || "").trim();
            return /send\s*(?:code|sms|text)|text\s*me|get\s*(?:a\s*)?code|发送|获取|发送验证码/i.test(text);
          });
          if (sendBtn) {
            log("clicking send code button:", sendBtn.textContent.trim());
            sendBtn.click();
            sendCodeClicked = true;
            continue;
          }
        }

        if (currentOtpInputs.length > 0) {
          await updateRuntime({ message: "检测到验证码输入框，正在获取验证码..." });
          const smsResult = await executePayPalSms(profile);
          log("OTP result: " + smsResult.message);
          await updateRuntime({
            status: smsResult.ok ? "running" : "waiting",
            stage: STAGES.PAYPAL_SMS,
            stageLabel: STAGE_LABELS[STAGES.PAYPAL_SMS],
            message: smsResult.message
          });
          return true;
        }
      }
      await updateRuntime({ status: "waiting", message: "未检测到短信验证页面" });
      return true;
    } finally {
      checkoutWebDirectRunning = false;
    }
  }

  function watchCheckoutWebRoute() {
    if (checkoutWebWatchStarted) return;
    checkoutWebWatchStarted = true;
    let ticks = 0;
    let fillAttempted = false;
    const timer = setInterval(() => {
      ticks += 1;
      if (isPayPalCheckoutWeb() && !hasOtpInputs() && checkoutWebBillingFormStillVisible() && !fillAttempted) {
        fillAttempted = true;
        runCheckoutWebUserscriptFlow({ force: true }).then(() => {
          clearInterval(timer);
        }).catch((error) => {
          updateRuntime({
            status: "error",
            stage: STAGES.ERROR,
            stageLabel: STAGE_LABELS[STAGES.ERROR],
            message: error?.message || String(error)
          });
          clearInterval(timer);
        });
      }
      if (ticks > 30) clearInterval(timer);
    }, 1000);
  }

  async function fillCheckoutWebFormWhenReady(profile, address) {
    // 只记录 URL 地区差异，不再刷新页面；地区按油猴脚本在表单内切到 US。
    if (normalizePayPalCheckoutUrl()) {
      return { ready: false, filled: 0, missing: ["url_redirected"] };
    }

    // 填写字段：重置 React _valueTracker + 原生 setter + 事件派发
    function fillField(id, val) {
      if (!val) return false;
      const el = document.getElementById(id);
      if (!el) { log("NOT FOUND", id); return false; }
      setReactValue(el, val);
      log(id, "=", el.value);
      return true;
    }

    function fillSelectField(id, text) {
      const el = document.getElementById(id);
      if (!el) { log("NOT FOUND", id); return false; }
      if (el instanceof HTMLSelectElement) {
        const lower = String(text || "").toLowerCase();
        for (let i = 0; i < el.options.length; i++) {
          const opt = el.options[i];
          if (opt.text.toLowerCase().includes(lower) || opt.value.toLowerCase().includes(lower)) {
            setReactValue(el, opt.value);
            log(id, "=", opt.text);
            return true;
          }
        }
      }
      return fillField(id, text);
    }

    // 1. 设置 country = US（重置 React _valueTracker）
    const country = document.getElementById("country");
    if (country && String(country.value || "").toUpperCase() !== "US") {
      setReactValue(country, "US");
      log("Country -> US, waiting 3s...");
      await sleep(3000);
    }

    // 2. 填写所有字段（和油猴脚本 doFill 完全一致）
    let filled = 0;
    filled += fillField("email", profile.email) ? 1 : 0;
    filled += fillField("phone", formatPhone(profile.phone)) ? 1 : 0;
    filled += fillField("cardNumber", profile.card.number) ? 1 : 0;
    filled += fillField("cardExpiry", profile.card.expiry) ? 1 : 0;
    filled += fillField("cardCvv", profile.card.cvv) ? 1 : 0;
    filled += fillField("password", profile.password) ? 1 : 0;
    filled += fillField("firstName", profile.firstName) ? 1 : 0;
    filled += fillField("lastName", profile.lastName) ? 1 : 0;
    // PayPal 部分页面有 full-name 合并字段
    const fullNameEl = document.getElementById("full-name");
    if (fullNameEl && !fullNameEl.value) fillField("full-name", `${profile.firstName} ${profile.lastName}`);
    filled += fillField("billingLine1", address.line1) ? 1 : 0;
    filled += fillField("billingCity", address.city) ? 1 : 0;
    filled += fillField("billingPostalCode", address.postalCode) ? 1 : 0;
    filled += fillSelectField("billingState", address.state) ? 1 : 0;
    log("filled", filled, "fields");

    // 3. 检查必填字段
    const missing = ["email", "phone", "cardNumber", "cardExpiry", "cardCvv", "billingLine1", "billingCity", "billingPostalCode"]
      .filter((id) => !document.getElementById(id));
    const unfilled = Object.entries({
      email: (v) => /@/.test(v) && v.trim().length >= 5,
      phone: (v) => v.replace(/\D/g, "").length >= 10,
      cardNumber: (v) => v.replace(/\D/g, "").length >= 12,
      cardExpiry: (v) => /\d{1,2}\D*\d{2,4}/.test(v),
      cardCvv: (v) => v.replace(/\D/g, "").length >= 3,
      billingLine1: (v) => v.trim().length >= 4,
      billingCity: (v) => v.trim().length >= 2,
      billingPostalCode: (v) => v.replace(/\D/g, "").length >= 5
    }).filter(([id, check]) => {
      const el = document.getElementById(id);
      return el && !check(String(el.value || ""));
    }).map(([id]) => id);

    return { ready: missing.length === 0 && unfilled.length === 0, filled, missing: [...missing, ...unfilled] };
  }

  function checkoutWebRequiredMissing() {
    return ["email", "phone", "cardNumber", "cardExpiry", "cardCvv", "billingLine1", "billingCity", "billingPostalCode"]
      .filter((id) => !byIdDeep(id));
  }

  function fillCheckoutWebForm(profile, address) {
    let filled = 0;
    filled += fillById("email", profile.email) ? 1 : 0;
    filled += fillById("phone", formatPhone(profile.phone)) ? 1 : 0;
    filled += fillById("cardNumber", profile.card.number) ? 1 : 0;
    filled += fillById("cardExpiry", profile.card.expiry) ? 1 : 0;
    filled += fillById("cardCvv", profile.card.cvv) ? 1 : 0;
    filled += fillById("password", profile.password) ? 1 : 0;
    filled += fillById("firstName", profile.firstName) ? 1 : 0;
    filled += fillById("lastName", profile.lastName) ? 1 : 0;
    filled += fillById("billingLine1", address.line1) ? 1 : 0;
    filled += fillById("billingCity", address.city) ? 1 : 0;
    filled += fillById("billingPostalCode", address.postalCode) ? 1 : 0;
    filled += fillSelectById("billingState", address.state) ? 1 : 0;
    const missing = checkoutWebRequiredMissing();
    const unfilled = checkoutWebRequiredUnfilled(profile, address);
    return { ready: missing.length === 0 && unfilled.length === 0, filled, missing: [...missing, ...unfilled] };
  }

  function checkoutWebRequiredUnfilled(_profile, _address) {
    const checks = {
      email: (value) => /@/.test(value) && value.trim().length >= 5,
      phone: (value) => value.replace(/\D/g, "").length >= 10,
      cardNumber: (value) => value.replace(/\D/g, "").length >= 12,
      cardExpiry: (value) => /\d{1,2}\D*\d{2,4}/.test(value),
      cardCvv: (value) => value.replace(/\D/g, "").length >= 3,
      billingLine1: (value) => value.trim().length >= 4,
      billingCity: (value) => value.trim().length >= 2,
      billingPostalCode: (value) => value.replace(/\D/g, "").length >= 5
    };
    return Object.entries(checks).filter(([id, check]) => {
      const el = byIdDeep(id);
      if (!el) return false;
      if (!("value" in el)) return true;
      const value = String(el.value || "");
      const nativeInvalid = typeof el.checkValidity === "function" && !el.checkValidity();
      return nativeInvalid || !check(value);
    }).map(([id]) => id);
  }

  async function fillOtpInMainWorld(code, submit) {
    try {
      const response = await chrome.runtime.sendMessage({
        type: "PAYPAL_AUTOFILL_MAIN_WORLD_OTP",
        payload: { code, submit }
      });
      if (response?.ok && response.result?.ok) return response.result;
      return { ok: false, message: response?.result?.message || response?.error || "main world otp fill failed" };
    } catch (error) {
      return { ok: false, message: error?.message || String(error) };
    }
  }

  function fillOtp(code, options = {}) {
    const digits = String(code || "").replace(/\D/g, "");
    if (!digits) return false;
    const inputs = findOtpInputs();
    if (!inputs.length) {
      if (!options.silent) log("no OTP inputs found");
      return false;
    }
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;

    function setVal(el, val) {
      if (setter) setter.call(el, val); else el.value = val;
    }

    if (inputs.length === 1) {
      const el = inputs[0];
      el.focus();
      setVal(el, digits);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      log("OTP filled into single input");
      return true;
    }

    // 多格 OTP：参考 checkout-personal-filler，派发完整键盘事件链
    const count = Math.min(inputs.length, digits.length);
    for (let i = 0; i < count; i += 1) {
      const el = inputs[i];
      const ch = digits[i];
      el.focus();
      el.dispatchEvent(new KeyboardEvent("keydown", { key: ch, code: "Digit" + ch, bubbles: true }));
      el.dispatchEvent(new InputEvent("beforeinput", { inputType: "insertText", data: ch, bubbles: true, cancelable: true }));
      setVal(el, ch);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      el.dispatchEvent(new KeyboardEvent("keyup", { key: ch, code: "Digit" + ch, bubbles: true }));
    }
    // 聚焦最后一个输入框
    if (inputs[count]) inputs[count].focus();
    log("OTP filled into", count, "inputs");
    return true;
  }

  async function tryApplyOtpFillRequest(request, attempt = 1) {
    const digits = String(request?.code || "").replace(/\D/g, "");
    if (!digits) return false;
    const shouldSubmit = request.submit !== false;

    const filled = fillOtp(digits, { silent: true });
    if (filled) {
      const submitted = shouldSubmit && await clickByPatternsWithRetry(ACTION_WORDS.next, 3);
      log("OTP watcher filled in content frame, submitted:", Boolean(submitted));
      return true;
    }

    if (attempt === 1 || attempt % 4 === 0) {
      const mainResult = await fillOtpInMainWorld(digits, shouldSubmit);
      if (mainResult?.ok && mainResult.filled) {
        log("OTP watcher filled in main world, inputs:", mainResult.found, "submitted:", Boolean(mainResult.submitted));
        return true;
      }
    }
    return false;
  }

  function handleOtpFillRequest(request) {
    if (!request?.code || !request.id) return;
    if (request.id === lastOtpFillRequestId) return;
    const createdAt = Number(request.createdAt || 0);
    if (createdAt && Date.now() - createdAt > OTP_REQUEST_MAX_AGE_MS) return;
    lastOtpFillRequestId = request.id;

    let attempts = 0;
    const tryFill = async () => {
      attempts += 1;
      try {
        if (await tryApplyOtpFillRequest(request, attempts)) return;
      } catch (error) {
        if (attempts === 1 || attempts % 10 === 0) log("OTP watcher attempt failed:", error?.message || String(error));
      }
      if (attempts === 1 || attempts % 10 === 0) {
        log("OTP watcher waiting for input frame, attempt:", attempts + "/" + OTP_FRAME_RETRY_ATTEMPTS);
      }
      if (attempts < OTP_FRAME_RETRY_ATTEMPTS) setTimeout(tryFill, OTP_FRAME_RETRY_DELAY_MS);
    };
    tryFill();
  }

  function startOtpFillWatcher() {
    storageGet(OTP_FILL_REQUEST_KEY).then((data) => {
      const request = data?.[OTP_FILL_REQUEST_KEY];
      if (request?.code) handleOtpFillRequest(request);
    }).catch(() => {});

    chrome.storage.onChanged.addListener((changes, area) => {
      if (area !== "local" || !changes[OTP_FILL_REQUEST_KEY]) return;
      const request = changes[OTP_FILL_REQUEST_KEY].newValue;
      if (request?.code) handleOtpFillRequest(request);
    });
  }

  async function requestOtpFillAcrossFrames(code, source = "manual", submit = true) {
    const digits = String(code || "").replace(/\D/g, "");
    if (!digits) return { ok: false, queued: false, message: "No OTP code" };
    try {
      await chrome.runtime.sendMessage({ type: "PAYPAL_AUTOFILL_INJECT_ALL_FRAMES" });
    } catch (_) {}
    const request = {
      id: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
      createdAt: Date.now(),
      code: digits,
      source,
      submit: submit !== false
    };
    await storageSet({ [OTP_FILL_REQUEST_KEY]: request, [SAVED_OTP_KEY]: digits });
    handleOtpFillRequest(request);
    return { ok: true, queued: true, message: "OTP fill request queued for all frames" };
  }

  function beginOtpCodeFetch(profile, source = "auto") {
    if (!profile?.otpUrl || otpCodeFetchRunning) return false;
    otpCodeFetchRunning = true;
    (async () => {
      try {
        log("OTP fetch worker started:", source);
        const code = await pollOtpCode(profile.otpUrl, Math.max(OTP_POLL_ATTEMPTS, 60));
        if (!code) {
          log("OTP fetch worker timed out");
          await updateRuntime({ status: "waiting", stage: STAGES.PAYPAL_SMS, stageLabel: STAGE_LABELS[STAGES.PAYPAL_SMS], message: "OTP not received yet" });
          return;
        }
        const result = await requestOtpFillAcrossFrames(code, source, profile.options?.autoSubmit !== false);
        log("OTP fill request broadcast:", result.message);
        await updateRuntime({ status: "running", stage: STAGES.PAYPAL_SMS, stageLabel: STAGE_LABELS[STAGES.PAYPAL_SMS], message: result.message });
      } catch (error) {
        log("OTP fetch worker failed:", error?.message || String(error));
        await updateRuntime({ status: "error", stage: STAGES.ERROR, stageLabel: STAGE_LABELS[STAGES.ERROR], message: error?.message || String(error) });
      } finally {
        otpCodeFetchRunning = false;
      }
    })();
    return true;
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

  async function executePayPalSms(profile) {
    if (!profile.otpUrl) return { ok: false, action: "blocked", message: "未配置验证码链接" };
    const code = await pollOtpCode(profile.otpUrl);
    if (!code) return { ok: false, action: "waiting", message: "未获取到短信验证码" };
    const result = await requestOtpFillAcrossFrames(code, "manual", profile.options.autoSubmit);
    return { ok: result.ok, code, action: result.queued ? "queued" : "filled", message: result.message };
  }

  async function executePayPalApprove(profile) {
    checkTerms();
    const consentButton = all("#consentButton, button[name='consentButton'], [data-testid='submit-button']")
      .find((button) => /agree|continue|authorize|同意|继续|授权/i.test(textOf(button)) || button.id === "consentButton");
    let clicked = false;
    if (profile.options.autoSubmit) {
      if (consentButton) {
        consentButton.click();
        clicked = true;
      } else {
        clicked = await clickByPatternsWithRetry(ACTION_WORDS.approve, 10);
      }
    }
    return { ok: clicked, action: clicked ? "submitted" : "waiting", message: clicked ? "已点击 PayPal 授权/继续" : "未找到授权按钮" };
  }

  async function executeCurrentStage(stage, profile) {
    switch (stage) {
      case STAGES.OPENAI_CHECKOUT:
      case STAGES.OPENAI_BILLING:
      case STAGES.OPENAI_SUBMIT:
        return executeOpenAiCheckout(profile);
      case STAGES.PAYPAL_LOGIN_EMAIL:
        return executePayPalLoginEmail(profile);
      case STAGES.PAYPAL_LOGIN_PASSWORD:
        return executePayPalLoginPassword(profile);
      case STAGES.PAYPAL_GUEST:
        return executePayPalGuest(profile);
      case STAGES.PAYPAL_SMS:
        return executePayPalSms(profile);
      case STAGES.PAYPAL_REVIEW:
      case STAGES.PAYPAL_APPROVE:
        return executePayPalApprove(profile);
      case STAGES.DONE:
        return { ok: true, action: "done", message: "流程已完成" };
      case STAGES.BLOCKED:
        return { ok: false, action: "blocked", message: "检测到验证码/风控，需要人工处理" };
      default:
        return { ok: false, action: "unknown", message: "当前页面不在支付流程内" };
    }
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
      const pool = profile.phonePool.length ? profile.phonePool : (profile.phone ? [normalizePhoneEntry({ phone: profile.phone, otpUrl: profile.otpUrl })] : []);
      if (pool.length > 1) nextState.phoneIndex = (Number(state.phoneIndex || 0) + 1) % pool.length;
    }
    await storageSet({ [STATE_KEY]: nextState });
    return nextState;
  }

  async function runFlow({ singleStep = false } = {}) {
    if (running) return { ok: false, message: "流程正在运行" };
    running = true;
    const runId = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    try {
      const { profile } = await readProfile();
      if (!profile.enabled) return { ok: false, message: "插件已禁用" };
      await updateRuntime({ runId, status: "running", stage: STAGES.DETECT, stageLabel: STAGE_LABELS[STAGES.DETECT], message: "开始识别页面", steps: [] });
      let lastStage = "";
      const steps = [];
      for (let i = 0; i < (singleStep ? 1 : 18); i += 1) {
        const stage = detectStage();
        const stageLabel = STAGE_LABELS[stage] || stage;
        await updateRuntime({ status: "running", stage, stageLabel, message: `执行：${stageLabel}`, steps });
        const result = await executeCurrentStage(stage, profile);
        steps.push({ stage, stageLabel, ok: Boolean(result.ok), action: result.action || "", message: result.message || "", at: Date.now() });
        await updateRuntime({ status: result.ok ? "running" : "waiting", stage, stageLabel, message: result.message || "", steps: steps.slice(-12) });
        if (stage === STAGES.DONE || result.action === "done") {
          if (profile.options.rotateOnSuccess) await advancePools({ card: true, phone: true });
          await updateRuntime({ status: "done", stage: STAGES.DONE, stageLabel: STAGE_LABELS[STAGES.DONE], message: "支付流程完成", steps: steps.slice(-12) });
          return { ok: true, message: "支付流程完成", steps };
        }
        if (singleStep || result.action === "blocked" || result.action === "unknown" || result.action === "waiting") {
          return { ok: Boolean(result.ok), message: result.message || stageLabel, stage, steps };
        }
        if (result.action === "submitted" || stage !== lastStage) {
          await sleep(FLOW_TICK_DELAY_MS);
          lastStage = stage;
        } else {
          return { ok: Boolean(result.ok), message: result.message || stageLabel, stage, steps };
        }
      }
      return { ok: false, message: "状态机达到最大步数，已停止", steps };
    } catch (error) {
      const message = error?.message || String(error);
      await updateRuntime({ status: "error", stage: STAGES.ERROR, stageLabel: STAGE_LABELS[STAGES.ERROR], message });
      return { ok: false, message };
    } finally {
      running = false;
    }
  }

  function extractCode(text) {
    const raw = String(text || "").trim();
    if (!raw) return "";

    // 1. 尝试 JSON 解析
    try {
      const json = JSON.parse(raw);
      const code = extractCodeFromJson(json);
      if (code) return code;
    } catch (_) {}

    if (/paypal:\s*thanks\s+for\s+confirming\s+your\s+phone\s+number/i.test(raw) && !/security\s+code|verification\s+code|otp/i.test(raw)) {
      return "";
    }

    const paypalSpaced = raw.match(/paypal[\s\S]{0,40}?((?:\d[\s-]*){6})[\s\S]{0,40}?(?:security|verification)\s+code/i);
    if (paypalSpaced) return paypalSpaced[1].replace(/\D/g, "").slice(0, 6);

    // 2. 关键词邻近匹配（如 "code: 123456" 或 "123456 is your verification code"）
    const keywordMatch = raw.match(/(?:code|otp|验证码|verification)[:\s]*(\d{4,8})/i)
      || raw.match(/(\d{4,8})\s*(?:is your|is the|为你的|是你的)/i);
    if (keywordMatch) return keywordMatch[1];

    // 3. 提取所有 4-8 位数字，过滤掉年份、手机号等
    const candidates = [];
    const re = /(?<!\d)(\d{4,8})(?!\d)/g;
    let m;
    while ((m = re.exec(raw)) !== null) {
      const d = m[1];
      if (/^(19|20)\d{2,4}$/.test(d)) continue; // 年份
      if (/^1\d{6,}$/.test(d)) continue; // 手机号
      if (/^(\d)\1+$/.test(d)) continue; // 重复数字
      candidates.push(d);
    }
    // 优先 6 位，然后 4, 5, 7, 8
    const priority = [6, 4, 5, 7, 8];
    for (const len of priority) {
      const found = candidates.find((c) => c.length === len);
      if (found) return found;
    }
    return candidates[0] || "";
  }

  function extractCodeFromJson(obj, depth = 0) {
    if (depth > 8 || !obj) return "";
    if (typeof obj === "string" || typeof obj === "number") {
      const s = String(obj);
      if (/paypal:\s*thanks\s+for\s+confirming\s+your\s+phone\s+number/i.test(s) && !/security\s+code|verification\s+code|otp/i.test(s)) return "";
      const spaced = s.match(/paypal[\s\S]{0,40}?((?:\d[\s-]*){6})[\s\S]{0,40}?(?:security|verification)\s+code/i);
      if (spaced) return spaced[1].replace(/\D/g, "").slice(0, 6);
      const keyword = s.match(/(?:code|otp|verification|验证码)[:\s]*(\d{4,8})/i)
        || s.match(/(\d{4,8})\s*(?:is your|is the|是你的|为你的)/i);
      if (keyword) return keyword[1];
      const m = s.match(/(?<!\d)(\d{4,8})(?!\d)/);
      return m ? m[1] : "";
    }
    if (Array.isArray(obj)) {
      for (const item of obj) { const c = extractCodeFromJson(item, depth + 1); if (c) return c; }
    }
    if (typeof obj === "object") {
      const codeKeys = /code|otp|sms|message|msg|body|text|content|data|verification|passcode/i;
      const ignoreKeys = /^(?:phone|phone_number|mobile|order_id|orderid|id|token|url|link|expired?_date|expire|time|date|created_at|updated_at)$/i;
      for (const [k, v] of Object.entries(obj)) {
        if (codeKeys.test(k)) { const c = extractCodeFromJson(v, depth + 1); if (c) return c; }
      }
      for (const [k, v] of Object.entries(obj)) {
        if (ignoreKeys.test(k)) continue;
        const c = extractCodeFromJson(v, depth + 1); if (c) return c;
      }
    }
    return "";
  }

  async function getStateSnapshot() {
    const data = await storageGet([RUNTIME_KEY]);
    const stage = detectStage();
    return {
      ok: true,
      stage,
      stageLabel: STAGE_LABELS[stage] || stage,
      runtime: data[RUNTIME_KEY] || {},
      url: location.href
    };
  }

  function ensureMiniPanel() {
    const existing = document.getElementById("gpt-paypal-autofill-panel");
    if (existing) return existing;
    const style = document.createElement("style");
    style.textContent = `
      #gpt-paypal-autofill-panel{position:fixed;right:16px;top:96px;z-index:2147483647;width:236px;padding:12px;border:1px solid #d8dee6;border-radius:8px;background:#fff;color:#111;font:12px/1.35 ui-monospace,Consolas,"Microsoft YaHei UI",monospace;box-shadow:0 18px 48px rgba(15,23,42,.16)}
      #gpt-paypal-autofill-panel strong{display:block;margin:0 26px 8px 0;font-size:13px}
      #gpt-paypal-autofill-panel button{height:30px;border:1px solid #d8dee6;border-radius:6px;background:#fff;color:#111;font:inherit;font-weight:700;cursor:pointer}
      #gpt-paypal-autofill-panel button:hover{background:#f3f4f6}
      #gpt-paypal-autofill-panel [data-close]{position:absolute;right:8px;top:8px;width:24px;height:24px}
      #gpt-paypal-autofill-panel .grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
      #gpt-paypal-autofill-panel [data-state]{min-height:32px;margin:9px 0;padding:8px;border:1px solid #eceff3;border-radius:6px;background:#f8fafc;word-break:break-all}
      #gpt-paypal-autofill-panel ol{margin:8px 0 0 18px;padding:0;max-height:112px;overflow:auto;color:#4b5563}
    `;
    const panel = document.createElement("section");
    panel.id = "gpt-paypal-autofill-panel";
    panel.innerHTML = `
      <button data-close type="button">×</button>
      <strong>PayPal 状态机</strong>
      <div data-state>就绪</div>
      <div class="grid">
        <button data-run type="button">一键流程</button>
        <button data-step type="button">当前节点</button>
        <button data-otp type="button">取码</button>
        <button data-submit type="button">继续</button>
      </div>
      <ol data-steps></ol>`;
    document.documentElement.append(style, panel);
    const state = panel.querySelector("[data-state]");
    panel.querySelector("[data-close]").addEventListener("click", () => panel.remove());
    panel.querySelector("[data-run]").addEventListener("click", async () => {
      state.textContent = "执行中...";
      const result = await runFlow();
      state.textContent = result.message || "完成";
    });
    panel.querySelector("[data-step]").addEventListener("click", async () => {
      const result = await runFlow({ singleStep: true });
      state.textContent = result.message || "已执行";
    });
    panel.querySelector("[data-otp]").addEventListener("click", async () => {
      const { profile } = await readProfile();
      const result = await executePayPalSms(profile);
      state.textContent = result.message;
    });
    panel.querySelector("[data-submit]").addEventListener("click", () => {
      state.textContent = clickByPatterns(ACTION_WORDS.next) ? "已继续" : "未找到按钮";
    });
    getStateSnapshot().then((snapshot) => updateMiniPanel({ ...snapshot.runtime, stageLabel: snapshot.stageLabel, message: snapshot.stageLabel })).catch(() => {});
    return panel;
  }

  function toggleMiniPanel() {
    const panel = document.getElementById("gpt-paypal-autofill-panel");
    if (panel) panel.remove();
    else ensureMiniPanel();
    return { ok: true, message: "已切换页面浮窗" };
  }

  function updateMiniPanel(runtime) {
    const panel = document.getElementById("gpt-paypal-autofill-panel");
    if (!panel) return;
    const state = panel.querySelector("[data-state]");
    const steps = panel.querySelector("[data-steps]");
    state.textContent = `${runtime.stageLabel || ""}${runtime.message ? " · " + runtime.message : ""}`;
    steps.innerHTML = (runtime.steps || []).slice(-6)
      .map((step) => `<li>${step.ok ? "✓" : "·"} ${escapeHtml(step.stageLabel)}：${escapeHtml(step.message || step.action)}</li>`)
      .join("");
  }

  async function invokeMessage(message = {}) {
    if (message?.type === "PAYPAL_AUTOFILL_GET_STATE") {
      return getStateSnapshot();
    }
    if (message?.type === "PAYPAL_AUTOFILL_TOGGLE_PANEL") {
      return toggleMiniPanel();
    }
    if (message?.type === "PAYPAL_AUTOFILL_FILL" || message?.type === "PAYPAL_AUTOFILL_STEP") {
      return runFlow({ singleStep: true });
    }
    if (message?.type === "PAYPAL_AUTOFILL_RUN_ALL") {
      return runFlow();
    }
    if (message?.type === "PAYPAL_AUTOFILL_RUN_CHECKOUTWEB") {
      return runCheckoutWebUserscriptFlow({ force: Boolean(message.force) });
    }
    if (message?.type === "PAYPAL_AUTOFILL_FILL_OTP") {
      const code = message.code || "";
      return requestOtpFillAcrossFrames(code, "message", message.submit);
    }
    if (message?.type === "PAYPAL_AUTOFILL_POLL_OTP") {
      const { profile } = await readProfile();
      return executePayPalSms(profile);
    }
    if (message?.type === "PAYPAL_AUTOFILL_CONTINUE") {
      const ok = clickByPatterns(ACTION_WORDS.next);
      return { ok, message: ok ? "已继续" : "未找到按钮" };
    }
    return { ok: false, message: `未知消息：${message?.type || ""}` };
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (String(message?.type || "").startsWith("PAYPAL_AUTOFILL_")) {
      invokeMessage(message).then(sendResponse);
      return true;
    }
    return false;
  });

  window.PayPalAutofillStateMachine = {
    stages: STAGES,
    detectStage,
    runFlow,
    getStateSnapshot,
    invoke: invokeMessage
  };

  async function maybeAutoRun() {
    if (autoRunStarted) return;
    autoRunStarted = true;
    await sleep(AUTO_RUN_DELAY_MS);
    // checkoutweb 页面由 runCheckoutWebUserscriptFlow 处理，但 OTP 页面需要自动接码
    if (isPayPalCheckoutWeb() && !hasOtpInputs()) return;
    const { profile } = await readProfile();
    if (!profile.options.autoRun) return;
    const stage = detectStage();
    if ([STAGES.UNKNOWN, STAGES.IDLE, STAGES.DONE, STAGES.BLOCKED, STAGES.ERROR].includes(stage)) return;
    await runFlow();
  }

  const initialStage = detectStage();
  startOtpFillWatcher();
  updateRuntime({ stage: initialStage, stageLabel: STAGE_LABELS[initialStage] || initialStage, status: "ready", message: "页面已加载" }).catch(() => {});
  watchCheckoutWebRoute();
  runCheckoutWebUserscriptFlow().catch((error) => updateRuntime({
    status: "error",
    stage: STAGES.ERROR,
    stageLabel: STAGE_LABELS[STAGES.ERROR],
    message: error?.message || String(error)
  }));
  maybeAutoRun().catch((error) => updateRuntime({
    status: "error",
    stage: STAGES.ERROR,
    stageLabel: STAGE_LABELS[STAGES.ERROR],
    message: error?.message || String(error)
  }));
})();
