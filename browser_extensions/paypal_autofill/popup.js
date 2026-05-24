const STORAGE_KEY = "paypalAutofillProfile";
const STATE_KEY = "paypalAutofillState";
const RUNTIME_KEY = "paypalAutofillRuntime";
const OTP_POLL_ATTEMPTS = 15;
const OTP_POLL_INTERVAL_MS = 2000;
const DEFAULT_PROFILE = window.PAYPAL_AUTOFILL_PROFILE || {};

const statusEl = document.getElementById("status");
const stageLabelEl = document.getElementById("stageLabel");
const otpUrlEl = document.getElementById("otpUrl");
const phonePoolEl = document.getElementById("phonePool");
const cardPoolEl = document.getElementById("cardPool");
const cardSummaryEl = document.getElementById("cardSummary");
const phoneSummaryEl = document.getElementById("phoneSummary");
const stepsEl = document.getElementById("steps");

function setStatus(text) {
  statusEl.textContent = text || "";
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

function digitsOnly(value) {
  return String(value || "").replace(/\D/g, "");
}

function normalizePhoneEntry(entry) {
  if (typeof entry === "string") {
    const [phone = "", otpUrl = ""] = String(entry).split("|").map((part) => part.trim());
    return { phone, otpUrl };
  }
  if (!entry || typeof entry !== "object") return { phone: "", otpUrl: "" };
  return {
    phone: entry.phone || entry.number || "",
    otpUrl: entry.otpUrl || entry.url || entry.link || ""
  };
}

function normalizeCardEntry(entry) {
  if (typeof entry === "string") {
    const parts = String(entry).split("|").map((part) => part.trim()).filter(Boolean);
    if (parts.length >= 4) {
      return { number: digitsOnly(parts[0]), month: parts[1], year: parts[2], cvv: digitsOnly(parts[3]) };
    }
    if (parts.length === 3) {
      const expiryMatch = parts[1].match(/(\d{1,2})\D*(\d{2,4})/);
      return {
        number: digitsOnly(parts[0]),
        month: expiryMatch ? expiryMatch[1] : parts[1],
        year: expiryMatch ? expiryMatch[2] : "",
        cvv: digitsOnly(parts[2])
      };
    }
    return { number: digitsOnly(parts[0] || ""), month: "", year: "", cvv: "" };
  }
  if (!entry || typeof entry !== "object") return { number: "", month: "", year: "", cvv: "" };
  return {
    number: digitsOnly(entry.number || entry.cardNumber || ""),
    month: String(entry.month || entry.expiryMonth || entry.expMonth || "").trim(),
    year: String(entry.year || entry.expiryYear || entry.expYear || "").trim(),
    cvv: digitsOnly(entry.cvv || entry.cardCvv || ""),
    expiry: String(entry.expiry || "").trim()
  };
}

function formatCardEntry(card) {
  const number = digitsOnly(card?.number || "");
  const month = String(card?.month || "").trim();
  const year = String(card?.year || "").trim();
  const cvv = digitsOnly(card?.cvv || "");
  if (number && month && year && cvv) return `${number}|${month}|${year}|${cvv}`;
  const expiry = String(card?.expiry || "").trim();
  if (number && expiry && cvv) {
    const match = expiry.match(/(\d{1,2})\D*(\d{2,4})/);
    if (match) return `${number}|${match[1].padStart(2, "0")}|${match[2]}|${cvv}`;
    return `${number}|${expiry}|${cvv}`;
  }
  return [number, month || expiry, year, cvv].filter(Boolean).join("|");
}

function parseCardLine(line) {
  const parsed = normalizeCardEntry(line);
  return parsed.number ? parsed : null;
}

function maskCard(card) {
  const number = String(card?.number || "").replace(/\D/g, "");
  if (number.length < 8) return "无";
  return `${number.slice(0, 4)} **** ${number.slice(-4)}`;
}

function maskPhone(phone) {
  const raw = String(phone || "").trim();
  if (raw.length < 7) return raw || "无";
  return `${raw.slice(0, 4)}****${raw.slice(-4)}`;
}

function readFormProfile() {
  return {
    enabled: true,
    poolVersion: DEFAULT_PROFILE.poolVersion || "",
    otpUrl: otpUrlEl.value.trim(),
    phonePool: phonePoolEl.value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).map(normalizePhoneEntry).filter((item) => item.phone || item.otpUrl),
    cardPool: cardPoolEl.value.split(/\r?\n/).map(parseCardLine).filter(Boolean),
    options: {
      autoSubmit: true,
      rotateOnSuccess: true,
      useAddressApi: true,
      autoRun: DEFAULT_PROFILE.options?.autoRun === true
    }
  };
}

async function readState() {
  const data = await storageGet([STORAGE_KEY, STATE_KEY, RUNTIME_KEY]);
  const storedProfile = data[STORAGE_KEY] || {};
  const shouldSeedPools = DEFAULT_PROFILE.poolVersion && storedProfile.poolVersion !== DEFAULT_PROFILE.poolVersion;
  const profile = {
    ...DEFAULT_PROFILE,
    ...storedProfile,
    poolVersion: shouldSeedPools ? DEFAULT_PROFILE.poolVersion : storedProfile.poolVersion || DEFAULT_PROFILE.poolVersion || "",
    phone: shouldSeedPools ? DEFAULT_PROFILE.phone : storedProfile.phone || DEFAULT_PROFILE.phone || "",
    otpUrl: shouldSeedPools ? DEFAULT_PROFILE.otpUrl : storedProfile.otpUrl || DEFAULT_PROFILE.otpUrl || "",
    card: shouldSeedPools ? DEFAULT_PROFILE.card : storedProfile.card || DEFAULT_PROFILE.card || {},
    options: shouldSeedPools
      ? { ...(storedProfile.options || {}), ...(DEFAULT_PROFILE.options || {}) }
      : { ...(DEFAULT_PROFILE.options || {}), ...(storedProfile.options || {}) },
    phonePool: shouldSeedPools
      ? DEFAULT_PROFILE.phonePool || []
      : Array.isArray(storedProfile.phonePool) && storedProfile.phonePool.length ? storedProfile.phonePool : DEFAULT_PROFILE.phonePool || [],
    cardPool: shouldSeedPools
      ? DEFAULT_PROFILE.cardPool || []
      : Array.isArray(storedProfile.cardPool) && storedProfile.cardPool.length ? storedProfile.cardPool : DEFAULT_PROFILE.cardPool || []
  };
  return {
    profile,
    state: data[STATE_KEY] || {},
    runtime: data[RUNTIME_KEY] || {}
  };
}

async function writeForm() {
  const { profile, state, runtime } = await readState();
  otpUrlEl.value = profile.otpUrl || "";
  phonePoolEl.value = (profile.phonePool || []).map(normalizePhoneEntry).map((item) => [item.phone, item.otpUrl].filter(Boolean).join("|")).join("\n");
  cardPoolEl.value = (profile.cardPool || []).map(normalizeCardEntry).map(formatCardEntry).join("\n");
  renderSummary(profile, state);
  renderRuntime(runtime);
}

function renderSummary(profile, state) {
  const cards = profile.cardPool || [];
  const phones = profile.phonePool || [];
  const card = cards.length ? cards[Math.abs(Number(state.cardIndex || 0)) % cards.length] : profile.card;
  const phone = phones.length ? phones[Math.abs(Number(state.phoneIndex || 0)) % phones.length] : normalizePhoneEntry({ phone: profile.phone, otpUrl: profile.otpUrl });
  cardSummaryEl.textContent = maskCard(card);
  phoneSummaryEl.textContent = phone?.phone ? maskPhone(phone.phone) : "无";
}

function renderRuntime(runtime = {}) {
  stageLabelEl.textContent = runtime.stageLabel || "未识别";
  if (runtime.message) setStatus(runtime.message);
  const steps = Array.isArray(runtime.steps) ? runtime.steps.slice(-6) : [];
  stepsEl.innerHTML = steps.length
    ? steps.map((step) => `<li>${step.ok ? "✓" : "·"} ${escapeHtml(step.stageLabel || step.stage || "")}：${escapeHtml(step.message || step.action || "")}</li>`).join("")
    : "<li>暂无节点记录</li>";
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

async function saveProfile() {
  const { profile: oldProfile, state } = await readState();
  const nextProfile = { ...oldProfile, ...readFormProfile() };
  await storageSet({ [STORAGE_KEY]: nextProfile });
  renderSummary(nextProfile, state);
  setStatus("已保存");
  return nextProfile;
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function ensureContentScripts(tabId) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId, allFrames: true },
      files: ["profile.generated.js", "content.js"]
    });
  } catch (_) {
    // The extension can only inject into allowed http/https frames.
  }
}

function normalizeFrameResult(item) {
  const result = item?.result || {};
  return {
    ...result,
    frameId: item?.frameId ?? 0,
    stage: result.stage || result.runtime?.stage || "",
    stageLabel: result.stageLabel || result.runtime?.stageLabel || ""
  };
}

function frameScore(frame, messageType = "") {
  const stage = frame.stage || "";
  const base = {
    paypal_sms: 100,
    paypal_guest: 92,
    paypal_approve: 88,
    paypal_review: 84,
    paypal_login_password: 80,
    paypal_login_email: 76,
    openai_submit: 72,
    openai_billing: 68,
    openai_checkout: 64,
    blocked: 55,
    done: 35,
    unknown: 0,
    idle: 0
  }[stage] ?? 0;
  const smsBoost = /OTP|POLL/i.test(messageType) && stage === "paypal_sms" ? 40 : 0;
  const panelBoost = messageType === "PAYPAL_AUTOFILL_TOGGLE_PANEL" && frame.frameId === 0 ? 40 : 0;
  return base + smsBoost + panelBoost;
}

async function collectFrameStates(tabId) {
  await ensureContentScripts(tabId);
  const results = await chrome.scripting.executeScript({
    target: { tabId, allFrames: true },
    func: async () => {
      const machine = window.PayPalAutofillStateMachine;
      if (!machine?.getStateSnapshot) {
        return { ok: false, stage: "missing", stageLabel: "未注入", message: "状态机未注入" };
      }
      return machine.getStateSnapshot();
    }
  });
  return (results || []).map(normalizeFrameResult);
}

async function invokeFrame(tabId, frameId, message) {
  const target = frameId == null ? { tabId } : { tabId, frameIds: [frameId] };
  const results = await chrome.scripting.executeScript({
    target,
    args: [message],
    func: async (payload) => {
      const machine = window.PayPalAutofillStateMachine;
      if (!machine?.invoke) {
        return { ok: false, stage: "missing", stageLabel: "未注入", message: "状态机未注入" };
      }
      return machine.invoke(payload);
    }
  });
  return normalizeFrameResult(results?.[0]);
}

async function sendToContent(message) {
  const tab = await activeTab();
  if (!tab?.id) throw new Error("没有当前标签页");
  const frames = await collectFrameStates(tab.id);
  if (message.type === "PAYPAL_AUTOFILL_GET_STATE") {
    return frames.sort((a, b) => frameScore(b, message.type) - frameScore(a, message.type))[0] || { ok: false, message: "未找到可用支付页面" };
  }
  const target = frames.sort((a, b) => frameScore(b, message.type) - frameScore(a, message.type))[0];
  if (!target || frameScore(target, message.type) <= 0) throw new Error("未找到可执行状态机的支付页面");
  return invokeFrame(tab.id, target.frameId, message);
}

function extractCode(text) {
  const raw = String(text || "").trim();
  if (!raw) return "";
  try {
    const parsed = JSON.parse(raw);
    const code = extractCodeFromJson(parsed);
    if (code) return code;
  } catch (_) {}
  const keywordMatch = raw.match(/(?:code|otp|verification|验证码)[:\s]*(\d{4,8})/i)
    || raw.match(/(\d{4,8})\s*(?:is your|is the|为你的|是你的)/i);
  if (keywordMatch) return keywordMatch[1];
  const candidates = [];
  const re = /(?<!\d)(\d{4,8})(?!\d)/g;
  let match;
  while ((match = re.exec(raw)) !== null) {
    const digits = match[1];
    if (/^(19|20)\d{2,4}$/.test(digits)) continue;
    if (/^1\d{6,}$/.test(digits)) continue;
    if (/^(\d)\1+$/.test(digits)) continue;
    candidates.push(digits);
  }
  for (const len of [6, 4, 5, 7, 8]) {
    const found = candidates.find((item) => item.length === len);
    if (found) return found;
  }
  return candidates[0] || "";
}

function extractCodeFromJson(obj, depth = 0) {
  if (depth > 8 || obj == null) return "";
  if (typeof obj === "string" || typeof obj === "number") {
    const match = String(obj).match(/(?<!\d)(\d{4,8})(?!\d)/);
    return match ? match[1] : "";
  }
  if (Array.isArray(obj)) {
    for (const item of obj) {
      const code = extractCodeFromJson(item, depth + 1);
      if (code) return code;
    }
    return "";
  }
  if (typeof obj === "object") {
    const codeKeys = /code|otp|sms|message|msg|body|text|content|data|verification|passcode|html|raw/i;
    for (const [key, value] of Object.entries(obj)) {
      if (!codeKeys.test(key)) continue;
      const code = extractCodeFromJson(value, depth + 1);
      if (code) return code;
    }
    for (const value of Object.values(obj)) {
      const code = extractCodeFromJson(value, depth + 1);
      if (code) return code;
    }
  }
  return "";
}

async function rotate(kind) {
  const { profile, state } = await readState();
  const key = kind === "card" ? "cardIndex" : "phoneIndex";
  const pool = kind === "card" ? profile.cardPool || [] : profile.phonePool || [];
  if (!pool.length) {
    setStatus(kind === "card" ? "卡池为空" : "手机号池为空");
    return;
  }
  const nextState = { ...state, [key]: (Number(state[key] || 0) + 1) % pool.length };
  await storageSet({ [STATE_KEY]: nextState });
  renderSummary(profile, nextState);
  setStatus(kind === "card" ? "卡池已轮换" : "手机号池已轮换");
}

async function pollOtpCode(url, attempts = OTP_POLL_ATTEMPTS) {
  for (let i = 0; i < attempts; i += 1) {
    const fetched = await chrome.runtime.sendMessage({ type: "FETCH_OTP_SMS", url });
    const code = extractCode(fetched?.text || fetched?.error || "");
    if (code) return code;
    if (i < attempts - 1) await sleep(OTP_POLL_INTERVAL_MS);
  }
  return "";
}

async function refreshRuntime() {
  try {
    const response = await sendToContent({ type: "PAYPAL_AUTOFILL_GET_STATE" });
    const runtime = response?.runtime || {};
    renderRuntime({
      ...runtime,
      stage: response?.stage || runtime.stage,
      stageLabel: response?.stageLabel || runtime.stageLabel,
      message: runtime.message || response?.stageLabel || ""
    });
    if (response?.stageLabel) setStatus(`当前：${response.stageLabel}`);
  } catch (error) {
    const { runtime } = await readState();
    renderRuntime(runtime);
    setStatus(error?.message || "状态读取失败");
  }
}

async function runAll() {
  await saveProfile();
  setStatus("状态机执行中...");
  const response = await sendToContent({ type: "PAYPAL_AUTOFILL_RUN_ALL" });
  setStatus(response?.message || "一键流程已发送");
  await refreshRuntime();
}

async function runStep() {
  await saveProfile();
  const response = await sendToContent({ type: "PAYPAL_AUTOFILL_STEP" });
  setStatus(response?.message || "当前节点已执行");
  await refreshRuntime();
}

async function fillOtpAndContinue() {
  const profile = await saveProfile();
  const { state } = await readState();
  const phoneEntry = Array.isArray(profile.phonePool) && profile.phonePool.length
    ? profile.phonePool[Math.abs(Number(state.phoneIndex || 0)) % profile.phonePool.length]
    : normalizePhoneEntry({ phone: profile.phone, otpUrl: profile.otpUrl });
  const otpUrl = phoneEntry?.otpUrl || profile.otpUrl;
  if (!otpUrl) {
    setStatus("未找到验证码链接");
    return;
  }
  setStatus("正在轮询验证码...");
  const code = await pollOtpCode(otpUrl);
  if (!code) {
    setStatus("未获取到验证码");
    return;
  }
  const response = await sendToContent({ type: "PAYPAL_AUTOFILL_FILL_OTP", code, submit: true });
  setStatus(response?.message || `验证码 ${code}`);
  await refreshRuntime();
}

async function togglePanel() {
  await sendToContent({ type: "PAYPAL_AUTOFILL_TOGGLE_PANEL" });
  setStatus("已切换页面浮窗");
}

document.getElementById("save").addEventListener("click", saveProfile);
document.getElementById("runAll").addEventListener("click", runAll);
document.getElementById("step").addEventListener("click", runStep);
document.getElementById("fillOtp").addEventListener("click", fillOtpAndContinue);
document.getElementById("nextCard").addEventListener("click", () => rotate("card"));
document.getElementById("nextPhone").addEventListener("click", () => rotate("phone"));
document.getElementById("togglePanel").addEventListener("click", togglePanel);
document.getElementById("refreshState").addEventListener("click", refreshRuntime);

writeForm().then(refreshRuntime).catch((error) => setStatus(error?.message || "加载失败"));

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes[STORAGE_KEY] || changes[STATE_KEY]) {
    readState().then(({ profile, state }) => renderSummary(profile, state)).catch(() => {});
  }
  if (changes[RUNTIME_KEY]) {
    renderRuntime(changes[RUNTIME_KEY].newValue || {});
  }
});
