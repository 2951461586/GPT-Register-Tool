export type AccountSummary = {
  id: string;
  email: string;
  success: boolean;
  status: string;
  registerMethod: string;
  sessionType: string;
  planType: string;
  accountType: string;
  promotionStatus: string;
  refreshTokenStatus: string;
  atProbeStatusCode: string;
  accessTokenPresent: boolean;
  refreshTokenPresent: boolean;
  totpPresent: boolean;
  workspaceStatus: string;
  workspaceName: string;
  registrationState: string;
  registrationCountry: string;
  mailboxProvider: string;
  mailboxSource: string;
  batchId: string;
  createdAt: string;
  updatedAt: string;
};

export type AccountPage = {
  items: AccountSummary[];
  page: number;
  pageSize: number;
  total: number;
};

export type AccountStats = {
  total: number;
  trial: number;
  registered: number;
  attention: number;
};

export type JobState = "Queued" | "Running" | "Succeeded" | "Failed" | "Cancelled";

export type BackendJob = {
  id: string;
  kind: string;
  state: JobState;
  createdAt: string;
  startedAt?: string;
  completedAt?: string;
  exitCode?: number;
  timedOut: boolean;
  result?: unknown;
  error: string;
};

export type JobEvent = {
  sequence: number;
  type: string;
  timestamp: string;
  data: unknown;
};

export type RegistrationSource = "pool" | "phone" | "cfworker" | "remail" | "smailr";
