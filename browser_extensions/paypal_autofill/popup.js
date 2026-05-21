const STORAGE_KEY = "paypalAutofillProfile";
const STATE_KEY = "paypalAutofillState";
const OTP_POLL_ATTEMPTS = 12;
const OTP_POLL_INTERVAL_MS = 2000;

const statusEl = document.getElementById("status");
const otpUrlEl = document.getElementById("otpUrl");
const phonePoolEl = document.getElementById("phonePool");
const cardPoolEl = document.getElementById("cardPool");
const cardSummaryEl = document.getElementById("cardSummary");
const phoneSummaryEl = document.getElementById("phoneSummary");

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
      return {
        number: digitsOnly(parts[0]),
        month: parts[1],
        year: parts[2],
        cvv: digitsOnly(parts[3])
      };
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
    cvv: digitsOnly(entry.cvv || entry.cardCvv || "")
  };
}

function formatCardEntry(card) {
  const number = digitsOnly(card?.number || "");
  const month = String(card?.month || "").trim();
  const year = String(card?.year || "").trim();
  const cvv = digitsOnly(card?.cvv || "");
  if (number && month && year && cvv) {
    return `${number}|${month}|${year}|${cvv}`;
  }
  const expiry = String(card?.expiry || "").trim();
  if (number && expiry && cvv) {
    const match = expiry.match(/(\d{1,2})\D*(\d{2,4})/);
    if (match) {
      const normalizedYear = match[2].length === 4 ? match[2] : match[2].padStart(2, "0");
      return `${number}|${match[1].padStart(2, "0")}|${normalizedYear}|${cvv}`;
    }
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
    otpUrl: otpUrlEl.value.trim(),
    phonePool: phonePoolEl.value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).map(normalizePhoneEntry).filter((item) => item.phone || item.otpUrl),
    cardPool: cardPoolEl.value.split(/\r?\n/).map(parseCardLine).filter(Boolean)
  };
}

async function readState() {
  const data = await storageGet([STORAGE_KEY, STATE_KEY]);
  return {
    profile: data[STORAGE_KEY] || {},
    state: data[STATE_KEY] || {}
  };
}

async function writeForm() {
  const { profile, state } = await readState();
  otpUrlEl.value = profile.otpUrl || "";
  phonePoolEl.value = (profile.phonePool || []).map(normalizePhoneEntry).map((item) => [item.phone, item.otpUrl].filter(Boolean).join("|")).join("\n");
  cardPoolEl.value = (profile.cardPool || []).map(normalizeCardEntry).map(formatCardEntry).join("\n");
  updateSummary(profile, state);
}

function updateSummary(profile, state) {
  const cards = profile.cardPool || [];
  const phones = profile.phonePool || [];
  const card = cards.length ? cards[Math.abs(Number(state.cardIndex || 0)) % cards.length] : profile.card;
  const phone = phones.length ? phones[Math.abs(Number(state.phoneIndex || 0)) % phones.length] : normalizePhoneEntry({ phone: profile.phone, otpUrl: profile.otpUrl });
  cardSummaryEl.textContent = maskCard(card);
  phoneSummaryEl.textContent = phone?.phone ? maskPhone(phone.phone) : "无";
}

async function saveProfile() {
  const { profile: oldProfile, state } = await readState();
  const nextProfile = { ...oldProfile, ...readFormProfile() };
  await storageSet({ [STORAGE_KEY]: nextProfile });
  updateSummary(nextProfile, state);
  setStatus("已保存");
  return nextProfile;
}

async function refreshSummary() {
  const { profile, state } = await readState();
  updateSummary(profile, state);
  return { profile, state };
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function sendToContent(message) {
  const tab = await activeTab();
  if (!tab?.id) throw new Error("没有当前标签页");
  try {
    return await chrome.tabs.sendMessage(tab.id, message);
  } catch (_) {
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      files: ["profile.generated.js", "content.js"]
    });
    return chrome.tabs.sendMessage(tab.id, message);
  }
}

function extractCode(text) {
  const match = String(text || "").match(/(?<!\d)\d{4,8}(?!\d)/);
  return match ? match[0] : "";
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
  updateSummary(profile, nextState);
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

async function fill() {
  await saveProfile();
  const response = await sendToContent({ type: "PAYPAL_AUTOFILL_FILL" });
  setStatus(response?.message || "已发送");
}

async function runAll() {
  await saveProfile();
  const response = await sendToContent({ type: "PAYPAL_AUTOFILL_RUN_ALL" });
  setStatus(response?.message || "一键执行已发送");
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
  const code = await pollOtpCode(otpUrl);
  if (!code) {
    setStatus("未获取到验证码");
    return;
  }
  const response = await sendToContent({ type: "PAYPAL_AUTOFILL_FILL_OTP", code, submit: true });
  if (response?.ok) {
    await rotate("card");
    await rotate("phone");
  }
  setStatus(response?.message || `验证码 ${code}`);
}

async function togglePanel() {
  await sendToContent({ type: "PAYPAL_AUTOFILL_TOGGLE_PANEL" });
  setStatus("已切换浮窗");
}

document.getElementById("save").addEventListener("click", saveProfile);
document.getElementById("runAll").addEventListener("click", runAll);
document.getElementById("fill").addEventListener("click", fill);
document.getElementById("fillOtp").addEventListener("click", fillOtpAndContinue);
document.getElementById("nextCard").addEventListener("click", () => rotate("card"));
document.getElementById("nextPhone").addEventListener("click", () => rotate("phone"));
document.getElementById("togglePanel").addEventListener("click", togglePanel);

writeForm().catch((error) => setStatus(error?.message || "加载失败"));

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes[STORAGE_KEY] || changes[STATE_KEY]) {
    refreshSummary().catch(() => {});
  }
});
