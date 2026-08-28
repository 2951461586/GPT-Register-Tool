import type {
  AccountPage,
  AccountStats,
  BackendJob,
  JobEvent,
  RegistrationSource,
} from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({ error: response.statusText }));
    throw new Error(detail.error || `HTTP ${response.status}`);
  }
  if (response.status === 202 && response.headers.get("content-length") === "0") {
    return undefined as T;
  }
  return response.json() as Promise<T>;
}

export function accountQuery(params: {
  q: string;
  status: string;
  planType: string;
  promotionStatus: string;
  page: number;
  pageSize: number;
}): string {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== "") query.set(key, String(value));
  });
  return query.toString();
}

export function getAccounts(params: Parameters<typeof accountQuery>[0]): Promise<AccountPage> {
  return request(`/api/accounts?${accountQuery(params)}`);
}

export function getAccountStats(): Promise<AccountStats> {
  return request("/api/accounts/stats");
}

export function getJobs(): Promise<BackendJob[]> {
  return request("/api/jobs");
}

export function startRegistration(input: {
  source: RegistrationSource;
  count: number;
  workers: number;
  disable2Fa: boolean;
  checkPromotion: boolean;
}): Promise<BackendJob> {
  return request("/api/jobs/registrations", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function startHealth(accountIds: string[], workers: number): Promise<BackendJob> {
  return request("/api/jobs/account-health", {
    method: "POST",
    body: JSON.stringify({ accountIds, workers, autoRelogin: true }),
  });
}

export function startPromotion(accountIds: string[], workers: number): Promise<BackendJob> {
  return request("/api/jobs/account-promotions", {
    method: "POST",
    body: JSON.stringify({ accountIds, workers }),
  });
}

export function startQuota(accountId: string): Promise<BackendJob> {
  return request(`/api/jobs/accounts/${encodeURIComponent(accountId)}/quota-usage`, {
    method: "POST",
    body: JSON.stringify({ refreshTimeoutSeconds: 60 }),
  });
}

export async function cancelJob(jobId: string): Promise<void> {
  await request(`/api/jobs/${jobId}/cancel`, { method: "POST" });
}

export function watchJob(
  jobId: string,
  onEvent: (event: JobEvent) => void,
  onDisconnected: () => void,
): () => void {
  const source = new EventSource(`/api/jobs/${jobId}/events`, { withCredentials: true });
  const handler = (event: MessageEvent<string>) => {
    try {
      onEvent(JSON.parse(event.data) as JobEvent);
    } catch {
      onEvent({
        sequence: Number(event.lastEventId || 0),
        type: event.type,
        timestamp: new Date().toISOString(),
        data: event.data,
      });
    }
  };
  ["state", "progress", "log"].forEach((name) => source.addEventListener(name, handler as EventListener));
  source.onerror = () => {
    source.close();
    onDisconnected();
  };
  return () => source.close();
}
