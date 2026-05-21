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

function parseCardLine(line) {
  const parts = String(line || "").split(/[|,\t]/).map((part) => part.trim()).filter(Boolean);
  if (!parts.length) return null;
  const number = (parts[0] || "").replace(/\D/g, "");
  if (number.length < 12) return null;
  return {
    number,
    expiry: parts[1] || "",
    cvv: (parts[2] || "").replace(/\D/g, "")
  };
}

function maskCard(card) {
  const number = String(card?.number || "").replace(/\D/g, "");
  if (number.length < 8) return "none";
  return `${number.slice(0, 4)} **** ${number.slice(-4)}`;
}

function readFormProfile() {
  return {
    enabled: true,
    otpUrl: otpUrlEl.value.trim(),
    phonePool: phonePoolEl.value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean),
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
  phonePoolEl.value = (profile.phonePool || []).join("\n");
  cardPoolEl.value = (profile.cardPool || []).map((card) => `${card.number}|${card.expiry}|${card.cvv}`).join("\n");
  updateSummary(profile, state);
}

function updateSummary(profile, state) {
  const cards = profile.cardPool || [];
  const phones = profile.phonePool || [];
  const card = cards.length ? cards[Math.abs(Number(state.cardIndex || 0)) % cards.length] : profile.card;
  const phone = phones.length ? phones[Math.abs(Number(state.phoneIndex || 0)) % phones.length] : profile.phone;
  cardSummaryEl.textContent = maskCard(card);
  phoneSummaryEl.textContent = phone || "none";
}

async function saveProfile() {
  const { profile: oldProfile, state } = await readState();
  const nextProfile = { ...oldProfile, ...readFormProfile() };
  await storageSet({ [STORAGE_KEY]: nextProfile });
  updateSummary(nextProfile, state);
  setStatus("saved");
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
  if (!tab?.id) throw new Error("no active tab");
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
    setStatus(`${kind} pool empty`);
    return;
  }
  const nextState = { ...state, [key]: (Number(state[key] || 0) + 1) % pool.length };
  await storageSet({ [STATE_KEY]: nextState });
  updateSummary(profile, nextState);
  setStatus(`${kind} rotated`);
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
  setStatus(response?.message || "sent");
}

async function runAll() {
  await saveProfile();
  const response = await sendToContent({ type: "PAYPAL_AUTOFILL_RUN_ALL" });
  setStatus(response?.message || "run all sent");
}

async function fillOtpAndContinue() {
  const profile = await saveProfile();
  if (!profile.otpUrl) {
    setStatus("otp url missing");
    return;
  }
  const code = await pollOtpCode(profile.otpUrl);
  if (!code) {
    setStatus("otp not found");
    return;
  }
  const response = await sendToContent({ type: "PAYPAL_AUTOFILL_FILL_OTP", code, submit: true });
  setStatus(response?.message || `otp ${code}`);
}

async function togglePanel() {
  await sendToContent({ type: "PAYPAL_AUTOFILL_TOGGLE_PANEL" });
  setStatus("panel toggled");
}

document.getElementById("save").addEventListener("click", saveProfile);
document.getElementById("runAll").addEventListener("click", runAll);
document.getElementById("fill").addEventListener("click", fill);
document.getElementById("fillOtp").addEventListener("click", fillOtpAndContinue);
document.getElementById("nextCard").addEventListener("click", () => rotate("card"));
document.getElementById("nextPhone").addEventListener("click", () => rotate("phone"));
document.getElementById("togglePanel").addEventListener("click", togglePanel);

writeForm().catch((error) => setStatus(error?.message || "load failed"));

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes[STORAGE_KEY] || changes[STATE_KEY]) {
    refreshSummary().catch(() => {});
  }
});
