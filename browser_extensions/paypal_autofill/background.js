const OTP_ALLOWED_HOSTS = ["a.62-us.com", "it.tgflare.com", "mail-api.yuecheng.shop"];

function parseAllowedOtpUrl(rawUrl) {
  const url = new URL(String(rawUrl || "").trim());
  if (!["http:", "https:"].includes(url.protocol)) {
    throw new Error("OTP URL must be http/https");
  }
  if (!OTP_ALLOWED_HOSTS.includes(url.hostname)) {
    throw new Error(`Unsupported OTP host: ${url.hostname}`);
  }
  return url;
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "FETCH_OTP_SMS") {
    (async () => {
      try {
        const url = parseAllowedOtpUrl(message.url);
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 15000);
        const response = await fetch(url.href, {
          cache: "no-store",
          credentials: "omit",
          signal: controller.signal
        });
        clearTimeout(timer);
        const text = await response.text();
        sendResponse({ ok: response.ok, status: response.status, text });
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
