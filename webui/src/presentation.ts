import type { AccountSummary, BackendJob } from "./types";

export function accountHealthLabel(account: AccountSummary): string {
  if (account.atProbeStatusCode === "200") return "正常";
  if (account.atProbeStatusCode === "401") return "AT 失效";
  if (account.status) return account.status;
  return "未检测";
}

export function badgeTone(value: string): "success" | "danger" | "neutral" | "warning" {
  const normalized = value.toLowerCase();
  if (/(success|succeeded|active|正常|200|完成|有效)/.test(normalized)) return "success";
  if (/(failed|error|401|失效|停用|失败)/.test(normalized)) return "danger";
  if (/(running|queued|检测|运行|排队)/.test(normalized)) return "warning";
  return "neutral";
}

export function isJobTerminal(job: BackendJob): boolean {
  return ["Succeeded", "Failed", "Cancelled"].includes(job.state);
}

export function eventText(data: unknown): string {
  if (typeof data === "string") return data;
  if (!data || typeof data !== "object") return String(data ?? "");
  const item = data as Record<string, unknown>;
  if (typeof item.text === "string") return item.text;
  if (typeof item.detail === "string" && item.detail) {
    return `${String(item.stage || "progress")}: ${item.detail}`;
  }
  if (typeof item.state === "string") {
    return item.error ? `${item.state}: ${String(item.error)}` : item.state;
  }
  return JSON.stringify(item);
}
