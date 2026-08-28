import { describe, expect, it } from "vitest";
import { accountQuery } from "./api";
import { accountHealthLabel, badgeTone, eventText } from "./presentation";
import type { AccountSummary } from "./types";

const account = {
  id: "1",
  email: "user@example.test",
  atProbeStatusCode: "200",
  status: "",
} as AccountSummary;

describe("web workbench contracts", () => {
  it("creates bounded account query parameters", () => {
    expect(accountQuery({
      q: "user",
      status: "",
      planType: "free",
      promotionStatus: "",
      page: 2,
      pageSize: 50,
    })).toBe("q=user&planType=free&page=2&pageSize=50");
  });

  it("maps account and task statuses to WPF-style badges", () => {
    expect(accountHealthLabel(account)).toBe("正常");
    expect(badgeTone("AT 失效 / 401")).toBe("danger");
    expect(badgeTone("Running")).toBe("warning");
  });

  it("formats structured SSE data without exposing implementation fields", () => {
    expect(eventText({ stage: "probing", detail: "HTTP 200" })).toBe("probing: HTTP 200");
  });
});
