(function () {
  "use strict";

  const DEFAULT_PROFILE = window.PAYPAL_AUTOFILL_PROFILE || {};
  const STORAGE_KEY = "paypalAutofillProfile";
  const STATE_KEY = "paypalAutofillState";
  const RUNTIME_KEY = "paypalAutofillRuntime";
  const LOG_PREFIX = "[GPT PayPal Flow]";
  const OTP_POLL_INTERVAL_MS = 2000;
  const OTP_POLL_ATTEMPTS = 15;
  const FLOW_TICK_DELAY_MS = 850;

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
    country: ["#country", "#billingCountry", "select[name='country']", "select[autocomplete='country']", "input[name='country']", "input[autocomplete='country']"]
  };

  const ACTION_WORDS = {
    next: [/continue|next|agree|submit|confirm|pay|subscribe|done|log\s*in|sign\s*in/i, /继续|下一步|同意|提交|确认|支付|购买|订阅|完成|登录/i],
    paypal: [/paypal/i],
    approve: [/agree\s*(?:and)?\s*continue|accept|authorize|approve|continue|pay\s*now/i, /同意|继续|授权|确认|批准/i],
    login: [/login|log\s*in|sign\s*in|continue|next/i, /登录|登入|继续|下一步/i],
    sms: [/send\s*code|resend|text\s*me|sms|continue/i, /发送|重发|短信|验证码|继续/i]
  };

  let running = false;

  function log(...args) {
    console.log(LOG_PREFIX, ...args);
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
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
        useAddressApi: profile.options?.useAddressApi !== false
      }
    };
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
          cardPool: DEFAULT_PROFILE.cardPool
        }
      : { ...DEFAULT_PROFILE, ...rawStored };
    const base = normalizeProfile(source);
    const state = data[STATE_KEY] || {};

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
    return Array.from(document.querySelectorAll(selector)).filter(isVisible);
  }

  function candidates(selectors) {
    return selectors.flatMap((selector) => Array.from(document.querySelectorAll(selector))).filter(isVisible);
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
      if (nativeSet(el, value)) {
        log("filled", name);
        return true;
      }
    }
    return false;
  }

  function clickByPatterns(patterns, selector = "button, a, [role='button'], input[type='button'], input[type='submit'], [tabindex]") {
    const normalized = Array.isArray(patterns) ? patterns : [patterns];
    const target = all(selector).find((el) => normalized.some((pattern) => pattern.test(textOf(el))));
    if (!target) return false;
    target.click();
    return true;
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
    return all("input").filter((input) => {
      const max = Number(input.getAttribute("maxlength") || input.maxLength || 0);
      const text = textOf(input) + " " + textOf(input.parentElement || {});
      const type = String(input.type || "").toLowerCase();
      const mode = String(input.getAttribute("inputmode") || "").toLowerCase();
      if (/zip|postal|card|cvv|cvc|phone|email|name|address|city|state/i.test(text)) return false;
      return input.autocomplete === "one-time-code" ||
        /otp|verification|security code|one.?time|passcode|code|验证码|短信/i.test(text) ||
        ((max >= 4 && max <= 8) && /text|tel|number|password/.test(type || "text")) ||
        (max === 1 && /numeric|decimal|tel/.test(mode || type));
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
      line1: address.line1 || fetched.line1 || "",
      city: address.city || fetched.city || "",
      state: address.state || fetched.state || "",
      postalCode: address.postalCode || fetched.postalCode || "",
      country: address.country || fetched.country || "US"
    };
  }

  function formatPhone(phone) {
    const raw = String(phone || "").trim();
    const digits = raw.replace(/\D/g, "");
    if (digits.length === 11 && digits.startsWith("1")) return digits.slice(1);
    return raw;
  }

  async function executeOpenAiCheckout(profile) {
    const payPalButton = findPayPalMethodButton();
    if (payPalButton) payPalButton.click();
    await sleep(350);
    const address = await resolveAddress(profile);
    let filled = 0;
    filled += fillFirst("address.line1", SELECTORS.line1, address.line1) ? 1 : 0;
    filled += fillFirst("address.city", SELECTORS.city, address.city) ? 1 : 0;
    filled += fillFirst("address.postalCode", SELECTORS.postalCode, address.postalCode) ? 1 : 0;
    filled += fillFirst("address.state", SELECTORS.state, address.state) ? 1 : 0;
    filled += fillFirst("address.country", SELECTORS.country, address.country) ? 1 : 0;
    checkTerms();
    const submitted = profile.options.autoSubmit && clickByPatterns(ACTION_WORDS.next);
    return { ok: true, action: submitted ? "submitted" : "filled", message: submitted ? "OpenAI/Stripe 已提交" : `OpenAI/Stripe 已填 ${filled} 项` };
  }

  async function executePayPalLoginEmail(profile) {
    if (!profile.email) return { ok: false, action: "waiting", message: "未配置 PayPal 邮箱" };
    const input = findEmailField();
    const filled = input && nativeSet(input, profile.email);
    const submitted = profile.options.autoSubmit && clickByPatterns(ACTION_WORDS.login);
    return { ok: Boolean(filled || submitted), action: submitted ? "submitted" : "filled", message: submitted ? "PayPal 邮箱已提交" : "PayPal 邮箱已填" };
  }

  async function executePayPalLoginPassword(profile) {
    if (!profile.password) return { ok: false, action: "waiting", message: "未配置 PayPal 密码" };
    const input = findPasswordField();
    const filled = input && nativeSet(input, profile.password);
    const submitted = profile.options.autoSubmit && clickByPatterns(ACTION_WORDS.login);
    return { ok: Boolean(filled || submitted), action: submitted ? "submitted" : "filled", message: submitted ? "PayPal 密码已提交" : "PayPal 密码已填" };
  }

  async function executePayPalGuest(profile) {
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
    const submitted = profile.options.autoSubmit && clickByPatterns(ACTION_WORDS.next);
    return { ok: filled > 0 || submitted, action: submitted ? "submitted" : "filled", message: submitted ? "PayPal 填表已提交" : `PayPal 已填 ${filled} 项` };
  }

  function fillOtp(code) {
    const digits = String(code || "").replace(/\D/g, "");
    if (!digits) return false;
    const inputs = findOtpInputs();
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
    const filled = fillOtp(code);
    const submitted = filled && profile.options.autoSubmit && clickByPatterns(ACTION_WORDS.next);
    return { ok: filled, code, action: submitted ? "submitted" : "filled", message: submitted ? `验证码 ${code} 已提交` : `验证码 ${code} 已填入` };
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
        clicked = clickByPatterns(ACTION_WORDS.approve);
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
      const pool = profile.phonePool.length ? profile.phonePool : (profile.phone ? [profile.phone] : []);
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
    const match = String(text || "").match(/(?<!\d)\d{4,8}(?!\d)/);
    return match ? match[0] : "";
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
    if (message?.type === "PAYPAL_AUTOFILL_FILL_OTP") {
      const code = message.code || "";
      const ok = fillOtp(code);
      if (ok && message.submit) clickByPatterns(ACTION_WORDS.next);
      return { ok, message: ok ? "验证码已填入" : "未找到验证码输入框" };
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

  const initialStage = detectStage();
  updateRuntime({ stage: initialStage, stageLabel: STAGE_LABELS[initialStage] || initialStage, status: "ready", message: "页面已加载" }).catch(() => {});
})();
