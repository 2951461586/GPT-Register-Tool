try {
  if (typeof importScripts === "function" && !globalThis.PAYPAL_AUTOFILL_PROFILE) {
    importScripts("profile.generated.js");
  }
} catch (_) {}

const DEFAULT_PROFILE = globalThis.PAYPAL_AUTOFILL_PROFILE || {};
const OTP_ALLOWED_HOSTS = ["a.62-us.com", "it.tgflare.com", "mail-api.yuecheng.shop", "liziai.cloud"];
const CHECKOUTWEB_RE = /^https:\/\/(?:www\.)?paypal\.com\/checkoutweb\//i;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function parseAllowedOtpUrl(rawUrl) {
  const url = new URL(String(rawUrl || "").trim());
  if (!["http:", "https:"].includes(url.protocol)) {
    throw new Error("OTP URL must be http/https");
  }
  if (!isAllowedOtpHost(url.hostname)) {
    throw new Error(`Unsupported OTP host: ${url.hostname}`);
  }
  return url;
}

function isAllowedOtpHost(hostname) {
  const host = String(hostname || "").toLowerCase();
  const cfHost = cloudflareTempEmailHost();
  return OTP_ALLOWED_HOSTS.includes(host) || (cfHost && (host === cfHost || host.endsWith(`.${cfHost}`)));
}

function cloudflareTempEmailConfig() {
  const cfg = DEFAULT_PROFILE.cloudflareTempEmail || DEFAULT_PROFILE.cloudflare_temp_email || {};
  return cfg && typeof cfg === "object" ? cfg : {};
}

function cloudflareTempEmailHost() {
  const cfg = cloudflareTempEmailConfig();
  const baseUrl = String(cfg.baseUrl || cfg.base_url || "").trim() || "https://liziai.cloud";
  try {
    return new URL(baseUrl.startsWith("http") ? baseUrl : `https://${baseUrl}`).hostname.toLowerCase();
  } catch (_) {
    return "liziai.cloud";
  }
}

function cloudflareTempEmailBaseUrl() {
  const cfg = cloudflareTempEmailConfig();
  const raw = String(cfg.baseUrl || cfg.base_url || "").trim() || "https://liziai.cloud";
  try {
    const url = new URL(raw.startsWith("http") ? raw : `https://${raw}`);
    return url.origin + (url.pathname === "/" ? "" : url.pathname.replace(/\/+$/, ""));
  } catch (_) {
    return "https://liziai.cloud";
  }
}

function cloudflareTempEmailHeaders() {
  const cfg = cloudflareTempEmailConfig();
  const headers = { Accept: "application/json" };
  const adminAuth = String(cfg.adminAuth || cfg.admin_auth || "").trim();
  if (adminAuth) headers["x-admin-auth"] = adminAuth;
  return headers;
}

function cloudflareTempEmailHeaderVariants() {
  const cfg = cloudflareTempEmailConfig();
  const adminAuth = String(cfg.adminAuth || cfg.admin_auth || "").trim();
  const base = cloudflareTempEmailHeaders();
  if (!adminAuth) return [base];
  const bearer = { Accept: "application/json", Authorization: `Bearer ${adminAuth}` };
  const variants = [
    { Accept: "application/json", "x-admin-token": adminAuth },
    { Accept: "application/json", "X-Admin-Token": adminAuth },
    bearer
  ];
  const authHeader = String(cfg.authHeader || cfg.auth_header || "").toLowerCase();
  return authHeader.includes("bearer") || authHeader.includes("authorization")
    ? [bearer, base, ...variants.filter((item) => item !== bearer)]
    : [base, ...variants];
}

function parseCloudflareOtpTarget(rawUrl) {
  const raw = String(rawUrl || "").trim();
  if (!raw) return null;
  if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(raw)) return { address: raw.toLowerCase() };
  if (/^(?:cfmail|cloudflare-temp-email):/i.test(raw)) {
    const address = raw.replace(/^(?:cfmail|cloudflare-temp-email):\/{0,2}/i, "").trim().toLowerCase();
    return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(address) ? { address } : null;
  }
  try {
    const url = new URL(raw);
    const host = cloudflareTempEmailHost();
    if (host && (url.hostname.toLowerCase() === host || url.hostname.toLowerCase().endsWith(`.${host}`))) {
      return { url };
    }
  } catch (_) {}
  return null;
}

async function fetchCloudflareOtpText(rawUrl, signal) {
  const target = parseCloudflareOtpTarget(rawUrl);
  if (!target) return null;
  let url = target.url;
  if (!url) {
    const cfg = cloudflareTempEmailConfig();
    const listPath = String(cfg.listPath || cfg.list_path || "/admin/emails").trim() || "/admin/emails";
    url = new URL(`${cloudflareTempEmailBaseUrl()}${listPath.startsWith("/") ? "" : "/"}${listPath}`);
    url.searchParams.set("address", target.address);
    url.searchParams.set("to_address", target.address);
    url.searchParams.set("limit", "20");
    url.searchParams.set("pageSize", "20");
    url.searchParams.set("page", "1");
  }
  let last = null;
  for (const headers of cloudflareTempEmailHeaderVariants()) {
    const response = await fetch(url.toString(), { cache: "no-store", credentials: "omit", headers, signal });
    const text = await response.text();
    last = { ok: response.ok, status: response.status, text };
    if (response.ok && !/^\s*<!doctype html|^\s*<html/i.test(text)) break;
    if (![401, 403].includes(response.status) && !response.ok) break;
  }
  return last;
}

async function runCheckoutWebMainWorld(sender, payload) {
  const tabId = sender?.tab?.id;
  if (!tabId || tabId < 0) throw new Error("No sender tab");
  const frameId = Number.isInteger(sender.frameId) ? sender.frameId : 0;
  const results = await chrome.scripting.executeScript({
    target: { tabId, frameIds: [frameId] },
    world: "MAIN",
    args: [payload || {}],
    func: mainWorldCheckoutWebFill
  });
  return results?.[0]?.result || { ok: false, message: "No result" };
}

async function runCheckoutWebDebugger(sender, payload) {
  const tabId = sender?.tab?.id;
  if (!tabId || tabId < 0) throw new Error("No sender tab");
  if (!chrome?.debugger?.attach || !chrome?.debugger?.sendCommand) {
    throw new Error("Chrome debugger API unavailable");
  }
  const frameId = Number.isInteger(sender.frameId) ? sender.frameId : 0;
  const target = { tabId };
  let attached = false;
  try {
    await chrome.debugger.attach(target, "1.3");
    attached = true;
    await chrome.debugger.sendCommand(target, "Page.bringToFront").catch(() => {});

    const fieldValues = [
      ["email", [payload.email]],
      ["phone", [payload.phone, ...(Array.isArray(payload.phoneCandidates) ? payload.phoneCandidates : [])]],
      ["cardNumber", [payload.cardNumber]],
      ["cardExpiry", [payload.cardExpiry, ...(Array.isArray(payload.cardExpiryCandidates) ? payload.cardExpiryCandidates : [])]],
      ["cardCvv", [payload.cardCvv]],
      ["password", [payload.password]],
      ["firstName", [payload.firstName]],
      ["lastName", [payload.lastName]],
      ["billingLine1", [payload.address?.line1]],
      ["billingCity", [payload.address?.city]],
      ["billingPostalCode", [payload.address?.postalCode]]
    ];
    const typed = [];
    for (const [id, values] of fieldValues) {
      const candidates = uniqueTextCandidates(values);
      if (!candidates.length) continue;
      let accepted = null;
      let last = null;
      for (const text of candidates) {
        const focused = await runCheckoutDebuggerScript(tabId, frameId, debuggerFocusCheckoutField, [id]);
        if (!focused?.ok) {
          last = { id, ok: false, reason: focused?.reason || "not_found" };
          break;
        }
        await chrome.debugger.sendCommand(target, "Input.insertText", { text });
        await sleep(140);
        const current = await runCheckoutDebuggerScript(tabId, frameId, debuggerReadCheckoutField, [id]);
        last = {
          id,
          ok: Boolean(current?.ok && current.length > 0 && current.valid !== false),
          length: current?.length || 0,
          valid: current?.valid,
          validation: current?.validation || ""
        };
        if (last.ok) {
          accepted = last;
          break;
        }
      }
      typed.push(accepted || last || { id, ok: false, reason: "empty" });
    }

    await runCheckoutDebuggerScript(tabId, frameId, debuggerSelectCheckoutState, [payload.address?.state || ""]);
    await sleep(900);
    const snapshot = await runCheckoutDebuggerScript(tabId, frameId, debuggerCheckoutSnapshot, []);
    const result = normalizeCheckoutDebuggerSnapshot(snapshot);
    let submitted = false;
    if (result.ready && payload.autoSubmit !== false) {
      const button = await runCheckoutDebuggerScript(tabId, frameId, debuggerFindSubmitButtonCenter, []);
      if (button?.ok) {
        await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
          type: "mousePressed",
          x: button.x,
          y: button.y,
          button: "left",
          clickCount: 1
        });
        await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
          type: "mouseReleased",
          x: button.x,
          y: button.y,
          button: "left",
          clickCount: 1
        });
        submitted = true;
      }
    }
    return {
      ok: true,
      source: "debugger",
      ready: result.ready,
      filled: result.filled,
      missing: result.missing,
      invalid: result.invalid,
      submitted,
      typed
    };
  } finally {
    if (attached) await chrome.debugger.detach(target).catch(() => {});
  }
}

async function runCheckoutDebuggerScript(tabId, frameId, func, args = []) {
  const results = await chrome.scripting.executeScript({
    target: { tabId, frameIds: [frameId] },
    world: "MAIN",
    args,
    func
  });
  return results?.[0]?.result || null;
}

function uniqueTextCandidates(values) {
  const seen = new Set();
  return (Array.isArray(values) ? values : [values])
    .map((value) => String(value || "").trim())
    .filter((value) => value && !seen.has(value) && seen.add(value));
}

function normalizeCheckoutDebuggerSnapshot(snapshot) {
  const fields = Array.isArray(snapshot?.fields) ? snapshot.fields : [];
  const byId = Object.fromEntries(fields.map((field) => [field.id, field]));
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
  const invalid = Object.entries(checks)
    .filter(([id, check]) => {
      const field = byId[id];
      if (!field?.present) return false;
      return field.valid === false || !check(String(field.value || ""));
    })
    .map(([id]) => id);
  const missing = Object.entries(checks)
    .filter(([id]) => !byId[id]?.present)
    .map(([id]) => id)
    .concat(invalid);
  return {
    ready: missing.length === 0,
    filled: fields.filter((field) => field.present && String(field.value || "").length > 0).length,
    missing,
    invalid
  };
}

function debuggerFocusCheckoutField(id) {
  const el = document.getElementById(String(id || ""));
  if (!el) return { ok: false, reason: "not_found" };
  if (el.disabled || el.readOnly) return { ok: false, reason: "disabled" };
  try { el.scrollIntoView({ block: "center", inline: "center" }); } catch (_) {}
  try { el.focus({ preventScroll: true }); } catch (_) { try { el.focus(); } catch (__) {} }
  try {
    if (typeof el.select === "function") el.select();
    else if (typeof el.setSelectionRange === "function") el.setSelectionRange(0, String(el.value || "").length);
  } catch (_) {}
  return { ok: true };
}

function debuggerReadCheckoutField(id) {
  const el = document.getElementById(String(id || ""));
  const validity = el && typeof el.checkValidity === "function" ? el.checkValidity() : true;
  return {
    ok: Boolean(el),
    value: el && "value" in el ? String(el.value || "") : "",
    length: el && "value" in el ? String(el.value || "").length : 0,
    valid: Boolean(validity),
    validation: el && typeof el.validationMessage === "string" ? el.validationMessage : "",
    pattern: el?.getAttribute?.("pattern") || "",
    maxlength: el?.getAttribute?.("maxlength") || ""
  };
}

function debuggerSelectCheckoutState(text) {
  const el = document.getElementById("billingState");
  const wanted = String(text || "").trim().toLowerCase();
  if (!el || !wanted) return { ok: false };
  if (el instanceof HTMLSelectElement) {
    const option = Array.from(el.options || []).find((item) => {
      const label = `${item.textContent || ""} ${item.value || ""}`.toLowerCase();
      return label.includes(wanted);
    });
    if (!option) return { ok: false };
    el.value = option.value;
    el.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
    return { ok: true, value: el.value };
  }
  return { ok: false };
}

function debuggerCheckoutSnapshot() {
  const ids = ["email", "phone", "cardNumber", "cardExpiry", "cardCvv", "password", "firstName", "lastName", "billingLine1", "billingCity", "billingPostalCode", "billingState"];
  return {
    fields: ids.map((id) => {
      const el = document.getElementById(id);
      return {
        id,
        present: Boolean(el),
        value: el && "value" in el ? String(el.value || "") : "",
        visible: Boolean(el && el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0),
        valid: el && typeof el.checkValidity === "function" ? el.checkValidity() : true,
        validation: el && typeof el.validationMessage === "string" ? el.validationMessage : "",
        pattern: el?.getAttribute?.("pattern") || "",
        maxlength: el?.getAttribute?.("maxlength") || ""
      };
    })
  };
}

function debuggerFindSubmitButtonCenter() {
  const button = document.querySelector('button[data-testid="submit-button"]')
    || document.querySelector('button[data-testid="hosted-payment-submit-button"]')
    || document.querySelector('button[data-atomic-wait-intent="Submit_Email"]')
    || document.querySelector("button.SubmitButton--complete")
    || Array.from(document.querySelectorAll("button")).find((item) => /agree|create\s*account|sign\s*up|submit|continue|confirm/i.test(String(item.textContent || "")));
  if (!button || button.disabled) return { ok: false };
  const rect = button.getBoundingClientRect();
  if (!rect.width || !rect.height) return { ok: false };
  try { button.scrollIntoView({ block: "center", inline: "center" }); } catch (_) {}
  const next = button.getBoundingClientRect();
  return { ok: true, x: next.left + next.width / 2, y: next.top + next.height / 2 };
}

async function runOtpMainWorld(sender, payload) {
  const tabId = sender?.tab?.id;
  if (!tabId || tabId < 0) throw new Error("No sender tab");
  const frameId = Number.isInteger(sender.frameId) ? sender.frameId : 0;
  const results = await chrome.scripting.executeScript({
    target: { tabId, frameIds: [frameId] },
    world: "MAIN",
    args: [payload || {}],
    func: mainWorldOtpFill
  });
  return results?.[0]?.result || { ok: false, message: "No result" };
}

async function mainWorldCheckoutWebFill(payload) {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const textOf = (el) => [
    el?.textContent,
    el?.value,
    el?.getAttribute?.("aria-label"),
    el?.getAttribute?.("title"),
    el?.getAttribute?.("placeholder"),
    el?.getAttribute?.("name"),
    el?.getAttribute?.("data-testid"),
    el?.id
  ].filter(Boolean).join(" ").replace(/\s+/g, " ").trim();

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

  function byId(id) {
    const escaped = window.CSS?.escape ? CSS.escape(id) : String(id).replace(/([^\w-])/g, "\\$1");
    return document.getElementById(id) || queryDeepAll(`#${escaped}`)[0] || null;
  }

  function isUsText(text) {
    return /^(US|USA)$/i.test(String(text || "").trim()) || /united states|u\.s\.|美国|美國/i.test(String(text || ""));
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

  function setNativeValue(el, value) {
    if (!el || value == null || String(value).trim() === "") return false;
    const next = String(value);
    try { el.focus({ preventScroll: true }); } catch (_) {}
    if (el._valueTracker) el._valueTracker.setValue("");
    if (!(el instanceof HTMLSelectElement) && tryInsertText(el, next)) return true;
    if (el instanceof HTMLSelectElement) {
      const options = Array.from(el.options || []);
      const option = options.find((item) => String(item.value || "").toUpperCase() === next.toUpperCase())
        || options.find((item) => String(item.textContent || "").toLowerCase().includes(next.toLowerCase()))
        || options.find((item) => isUsText(`${item.textContent || ""} ${item.value || ""}`) && isUsText(next));
      el.value = option ? option.value : next;
      dispatchValueEvents(el, next);
      return true;
    }
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, next);
    else el.value = next;
    dispatchValueEvents(el, next);
    return true;
  }

  function tryInsertText(el, value) {
    if (typeof document.execCommand !== "function") return false;
    try {
      if (typeof el.select === "function") el.select();
      else if (typeof el.setSelectionRange === "function") el.setSelectionRange(0, String(el.value || "").length);
      const ok = document.execCommand("insertText", false, String(value));
      if (!ok || !String(el.value || "")) return false;
      el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
      el.dispatchEvent(new Event("blur", { bubbles: true, composed: true }));
      return true;
    } catch (_) {
      return false;
    }
  }

  function visible(el) {
    if (!el || el.disabled || el.getAttribute?.("aria-disabled") === "true") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function allVisible(selector) {
    return queryDeepAll(selector).filter(visible);
  }

  function findCountry() {
    return byId("country")
      || allVisible("#billingCountry, #billingCountrySelector, select[name*='country' i], input[name*='country' i], [role='combobox'][aria-label*='country' i], button[id*='country' i], button[aria-label*='country' i], [data-testid*='country' i]")[0]
      || allVisible("select, input, button, [role='combobox']").find((el) => /country|billing country|国家|地區|地区/i.test(textOf(el)));
  }

  function findCountryOption() {
    return allVisible("[role='option'], [data-value], li, button, div, span").find((item) => {
      const dataValue = String(item.getAttribute?.("data-value") || item.getAttribute?.("value") || "");
      return /^US$/i.test(dataValue) || isUsText(textOf(item));
    });
  }

  function forceUsCountry() {
    const country = findCountry();
    if (!country) return { found: false, changed: false, value: "" };
    const before = String(country.value || "");
    if (country instanceof HTMLSelectElement) {
      const options = Array.from(country.options || []);
      const option = options.find((item) => String(item.value || "").toUpperCase() === "US")
        || options.find((item) => isUsText(`${item.textContent || ""} ${item.value || ""}`));
      if (country._valueTracker) country._valueTracker.setValue("");
      country.value = option ? option.value : "US";
      dispatchValueEvents(country);
    } else if (country instanceof HTMLInputElement || country instanceof HTMLTextAreaElement) {
      country.click();
      setNativeValue(country, "US");
      const option = findCountryOption();
      if (option) option.click();
    } else {
      country.click();
      const option = findCountryOption();
      if (option) option.click();
    }
    return { found: true, changed: before !== String(country.value || ""), value: String(country.value || "") };
  }

  function fillId(id, value) {
    const el = byId(id);
    if (!el) return false;
    return setNativeValue(el, value);
  }

  function fillState(value) {
    const el = byId("billingState");
    if (!el) return false;
    if (el instanceof HTMLSelectElement) {
      const wanted = String(value || "").toLowerCase();
      const option = Array.from(el.options || []).find((item) => {
        const label = `${item.textContent || ""} ${item.value || ""}`.toLowerCase();
        return label.includes(wanted);
      });
      if (option) return setNativeValue(el, option.value);
    }
    return setNativeValue(el, value);
  }

  function requiredMissing() {
    return ["email", "phone", "cardNumber", "cardExpiry", "cardCvv", "billingLine1", "billingCity", "billingPostalCode"].filter((id) => !byId(id));
  }

  function requiredUnfilled() {
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
      const el = byId(id);
      if (!el) return false;
      if (!("value" in el)) return true;
      const value = String(el.value || "");
      const nativeInvalid = typeof el.checkValidity === "function" && !el.checkValidity();
      return nativeInvalid || !check(value);
    }).map(([id]) => id);
  }

  function checkTerms() {
    const include = /agree|agreement|authorize|authorization|consent|policy|terms|automatic|billing|同意|协议|条款|授权|政策|自动续费/i;
    const exclude = /newsletter|marketing|promo|offer|remember|save\s+my|保存信息|记住|营销/i;
    const box = allVisible("input[type='checkbox']").find((item) => !item.checked && include.test(textOf(item.parentElement || item)) && !exclude.test(textOf(item.parentElement || item)));
    if (!box) return false;
    box.click();
    return true;
  }

  function clickSubmit() {
    let btn = byId("submit-button")
      || document.querySelector('button[data-testid="submit-button"]')
      || document.querySelector('button[data-testid="hosted-payment-submit-button"]')
      || document.querySelector('button[data-atomic-wait-intent="Submit_Email"]')
      || document.querySelector("button.SubmitButton--complete");
    if (!btn) {
      btn = allVisible("button").find((item) => {
        const text = String(item.textContent || "").trim();
        return ["下一页", "Next", "Subscribe", "Pay", "Continue", "Agree"].includes(text);
      });
    }
    if (!btn || btn.disabled || !visible(btn)) return false;
    try { btn.scrollIntoView({ block: "center", inline: "center" }); } catch (_) {}
    try { btn.focus({ preventScroll: true }); } catch (_) {}
    for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
      try {
        const event = type.startsWith("pointer") && typeof PointerEvent === "function"
          ? new PointerEvent(type, { bubbles: true, cancelable: true, pointerType: "mouse", isPrimary: true })
          : new MouseEvent(type, { bubbles: true, cancelable: true, view: window });
        btn.dispatchEvent(event);
      } catch (_) {}
    }
    try { btn.click(); } catch (_) {}
    const form = btn.form || btn.closest?.("form") || document.querySelector("form");
    if (form) {
      try { form.requestSubmit(btn); } catch (_) {
        try { form.requestSubmit(); } catch (_) {}
      }
    }
    return true;
  }

  function fillOnce() {
    let filled = 0;
    filled += fillId("email", payload.email) ? 1 : 0;
    filled += fillId("phone", payload.phone) ? 1 : 0;
    filled += fillId("cardNumber", payload.cardNumber) ? 1 : 0;
    filled += fillId("cardExpiry", payload.cardExpiry) ? 1 : 0;
    filled += fillId("cardCvv", payload.cardCvv) ? 1 : 0;
    filled += fillId("password", payload.password) ? 1 : 0;
    filled += fillId("firstName", payload.firstName) ? 1 : 0;
    filled += fillId("lastName", payload.lastName) ? 1 : 0;
    filled += fillId("billingLine1", payload.address?.line1) ? 1 : 0;
    filled += fillId("billingCity", payload.address?.city) ? 1 : 0;
    filled += fillId("billingPostalCode", payload.address?.postalCode) ? 1 : 0;
    filled += fillState(payload.address?.state) ? 1 : 0;
    const missing = requiredMissing();
    const unfilled = requiredUnfilled();
    return { filled, ready: missing.length === 0 && unfilled.length === 0, missing: [...missing, ...unfilled] };
  }

  let country = { found: false, changed: false, value: "" };
  let result = { filled: 0, ready: false, missing: requiredMissing() };
  for (let i = 0; i < 36; i += 1) {
    const latestCountry = forceUsCountry();
    if (latestCountry.found) country = latestCountry;
    if (latestCountry.found && latestCountry.changed) await sleep(3000);
    result = fillOnce();
    if (result.ready) break;
    await sleep(1000);
  }

  let submitted = false;
  if (result.ready) {
    checkTerms();
    await sleep(500);
    if (payload.autoSubmit) submitted = clickSubmit();
  }

  return {
    ok: true,
    source: "main_world",
    country,
    ready: result.ready,
    filled: result.filled,
    missing: result.missing,
    submitted
  };
}

async function mainWorldOtpFill(payload) {
  const code = String(payload?.code || "").replace(/\D/g, "");
  const shouldSubmit = payload?.submit !== false;
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const textOf = (el) => [
    el?.textContent,
    el?.value,
    el?.getAttribute?.("aria-label"),
    el?.getAttribute?.("title"),
    el?.getAttribute?.("placeholder"),
    el?.getAttribute?.("name"),
    el?.getAttribute?.("autocomplete"),
    el?.getAttribute?.("data-testid"),
    el?.id
  ].filter(Boolean).join(" ").replace(/\s+/g, " ").trim();

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

  function visible(el) {
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

  function hints(el) {
    const parts = [
      el.id, el.name,
      el.getAttribute("aria-label"), el.getAttribute("placeholder"),
      el.getAttribute("autocomplete"), el.getAttribute("data-testid"),
      el.getAttribute("inputmode"), el.getAttribute("pattern")
    ];
    try { if (el.labels?.length) parts.push(el.labels[0].textContent); } catch (_) {}
    let parent = el.parentElement;
    for (let i = 0; i < 3 && parent; i += 1) {
      parts.push(parent.textContent);
      parent = parent.parentElement;
    }
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
    for (let i = 0; i < Math.min(els.length, 200); i += 1) text += " " + (els[i].textContent || "");
    return text;
  }

  function orderByPosition(inputs) {
    return inputs.slice().sort((a, b) => {
      const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
      return ra.top - rb.top || ra.left - rb.left;
    });
  }

  function findOtpInputs() {
    const allInputs = queryDeepAll("input").filter((el) => visible(el));
    const hosted = Array.from({ length: 6 }, (_, index) => document.getElementById(`ci-ciBasic-${index}`))
      .filter((el) => el && visible(el));
    if (hosted.length >= 6) return hosted;
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

    const multiLabel = allInputs.filter((el) => Number(el.getAttribute("maxlength") || el.maxLength || 0) === 1 && isOtpLabel(hints(el)));
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

  function dispatchTextInput(el, value) {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    el.focus();
    el.dispatchEvent(new KeyboardEvent("keydown", { key: value.slice(-1), bubbles: true }));
    if (typeof InputEvent === "function") {
      el.dispatchEvent(new InputEvent("beforeinput", { inputType: "insertText", data: value, bubbles: true, cancelable: true }));
    }
    if (setter) setter.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new KeyboardEvent("keyup", { key: value.slice(-1), bubbles: true }));
  }

  function clickNext() {
    const buttons = queryDeepAll("button, a, [role='button'], input[type='submit'], input[type='button']").filter(visible);
    const target = buttons.find((el) => /continue|next|submit|verify|confirm|done|send|同意|继续|下一步|提交|验证|确认/i.test(textOf(el)));
    if (!target) return false;
    target.click();
    return true;
  }

  if (!code) return { ok: false, found: 0, filled: false, submitted: false, message: "empty code" };
  const inputs = findOtpInputs();
  if (!inputs.length) return { ok: false, found: 0, filled: false, submitted: false, message: "no otp inputs" };

  if (inputs.length === 1) {
    dispatchTextInput(inputs[0], code);
  } else {
    const count = Math.min(inputs.length, code.length);
    for (let i = 0; i < count; i += 1) {
      dispatchTextInput(inputs[i], code[i]);
      await sleep(35);
    }
  }

  await sleep(150);
  const submitted = shouldSubmit ? clickNext() : false;
  return { ok: true, found: inputs.length, filled: true, submitted, message: submitted ? "otp submitted" : "otp filled" };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "PAYPAL_AUTOFILL_INJECT_ALL_FRAMES") {
    (async () => {
      try {
        const tabId = sender?.tab?.id;
        if (!tabId || tabId < 0) throw new Error("No sender tab");
        await chrome.scripting.executeScript({
          target: { tabId, allFrames: true },
          files: ["profile.generated.js", "content.js"]
        });
        sendResponse({ ok: true });
      } catch (error) {
        sendResponse({ ok: false, error: error?.message || String(error) });
      }
    })();
    return true;
  }

  if (message?.type === "PAYPAL_AUTOFILL_MAIN_WORLD_CHECKOUTWEB") {
    (async () => {
      try {
        const result = await runCheckoutWebMainWorld(sender, message.payload || {});
        sendResponse({ ok: true, result });
      } catch (error) {
        sendResponse({ ok: false, error: error?.message || String(error) });
      }
    })();
    return true;
  }

  if (message?.type === "PAYPAL_AUTOFILL_DEBUGGER_CHECKOUTWEB") {
    (async () => {
      try {
        const result = await runCheckoutWebDebugger(sender, message.payload || {});
        sendResponse({ ok: true, result });
      } catch (error) {
        sendResponse({ ok: false, error: error?.message || String(error) });
      }
    })();
    return true;
  }

  if (message?.type === "PAYPAL_AUTOFILL_MAIN_WORLD_OTP") {
    (async () => {
      try {
        const result = await runOtpMainWorld(sender, message.payload || {});
        sendResponse({ ok: true, result });
      } catch (error) {
        sendResponse({ ok: false, error: error?.message || String(error) });
      }
    })();
    return true;
  }

  if (message?.type === "FETCH_OTP_SMS") {
    (async () => {
      try {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 15000);
        const cfResult = await fetchCloudflareOtpText(message.url, controller.signal);
        if (cfResult) {
          clearTimeout(timer);
          sendResponse(cfResult);
          return;
        }
        const url = parseAllowedOtpUrl(message.url);
        const response = await fetch(url.href, { cache: "no-store", credentials: "omit", signal: controller.signal });
        clearTimeout(timer);
        sendResponse({ ok: response.ok, status: response.status, text: await response.text() });
      } catch (error) {
        sendResponse({ ok: false, error: error?.message || String(error) });
      }
    })();
    return true;
  }

  if (message?.type === "FETCH_US_ADDRESS") {
    fetch("https://www.meiguodizhi.com/api/v1/dz", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: "/", method: "address" })
    })
      .then((response) => response.json())
      .then((data) => {
        const address = data.address || data;
        sendResponse({
          ok: true,
          address: {
            line1: address.Address || address.street || "123 Main St",
            city: address.City || address.city || "New York",
            state: address.State_Full || address.State || address.state || "New York",
            postalCode: String(address.Zip_Code || address.zip || "10001").slice(0, 5),
            country: "US"
          }
        });
      })
      .catch(() => sendResponse({
        ok: false,
        address: { line1: "123 Main St", city: "New York", state: "New York", postalCode: "10001", country: "US" }
      }));
    return true;
  }
});

chrome.action.onClicked.addListener(async (tab) => {
  if (!tab?.id || !/^https?:\/\//i.test(tab.url || "")) return;
  try {
    await chrome.tabs.sendMessage(tab.id, { type: "PAYPAL_AUTOFILL_TOGGLE_PANEL" });
  } catch (_) {
    try {
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["profile.generated.js", "content.js"] });
      await chrome.tabs.sendMessage(tab.id, { type: "PAYPAL_AUTOFILL_TOGGLE_PANEL" });
    } catch (_) {
      // Restricted pages cannot be injected.
    }
  }
});

async function injectCheckoutContent(tabId) {
  if (!tabId || tabId < 0) return;
  try {
    await chrome.scripting.executeScript({
      target: { tabId, frameIds: [0] },
      files: ["profile.generated.js", "content.js"]
    });
  } catch (_) {
    // Main-frame injection can fail only on restricted pages.
  }
  try {
    await chrome.scripting.executeScript({
      target: { tabId, allFrames: true },
      files: ["profile.generated.js", "content.js"]
    });
  } catch (_) {
    // Some PayPal subframes may reject injection; the main checkout frame is enough.
  }
  try {
    await chrome.tabs.sendMessage(tabId, { type: "PAYPAL_AUTOFILL_RUN_CHECKOUTWEB", force: true });
  } catch (_) {
    // The content script may still be loading; the route watcher is the fallback.
  }
}

chrome.webNavigation.onCommitted.addListener((details) => {
  if (details.frameId !== 0 || !CHECKOUTWEB_RE.test(details.url || "")) return;
  injectCheckoutContent(details.tabId);
});

chrome.webNavigation.onHistoryStateUpdated.addListener((details) => {
  if (details.frameId !== 0 || !CHECKOUTWEB_RE.test(details.url || "")) return;
  injectCheckoutContent(details.tabId);
});
