// Thin, typed wrapper around the two backend endpoints this dashboard
// needs. Phase 10 added per-business API keys on the backend -- every
// call here now sends one as a Bearer token; this is the one place that
// changed, no call site elsewhere in the app needed to know.

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8010";

export type EscalationStatus = "OPEN" | "RESOLVED";

export interface EscalationQueueItem {
  id: string;
  processing_run_id: string;
  reason: string;
  status: EscalationStatus;
  created_at: string;
  resolved_at: string | null;
  customer_name: string;
  customer_contact: string;
  raw_message: string;
}

export class ApiError extends Error {}

async function parseErrorDetail(response: Response): Promise<string> {
  try {
    const body = await response.json();
    if (typeof body?.detail === "string") return body.detail;
  } catch {
    // response wasn't JSON -- fall through to the generic message below
  }
  return `${response.status} ${response.statusText}`;
}

function authHeaders(apiKey: string): HeadersInit {
  return { Authorization: `Bearer ${apiKey}` };
}

export async function fetchEscalations(
  businessId: string,
  apiKey: string,
  statusFilter?: EscalationStatus,
): Promise<EscalationQueueItem[]> {
  const params = new URLSearchParams({ business_id: businessId });
  if (statusFilter) params.set("status_filter", statusFilter);

  const response = await fetch(`${API_BASE_URL}/v1/escalations?${params}`, {
    headers: authHeaders(apiKey),
  });
  if (!response.ok) {
    throw new ApiError(await parseErrorDetail(response));
  }
  return response.json();
}

export async function resolveEscalation(
  escalationId: string,
  businessId: string,
  apiKey: string,
): Promise<EscalationQueueItem> {
  const params = new URLSearchParams({ business_id: businessId });
  const response = await fetch(
    `${API_BASE_URL}/v1/escalations/${escalationId}/resolve?${params}`,
    { method: "POST", headers: authHeaders(apiKey) },
  );
  if (!response.ok) {
    throw new ApiError(await parseErrorDetail(response));
  }
  return response.json();
}
