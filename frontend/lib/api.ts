// Thin, typed wrapper around the two backend endpoints this dashboard
// needs. No auth header yet -- the backend trusts a business_id the same
// way the rest of the API does today (see README, Phase 9); this is the
// one place that would gain an Authorization header once real login
// exists, without every call site changing.

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

export async function fetchEscalations(
  businessId: string,
  statusFilter?: EscalationStatus,
): Promise<EscalationQueueItem[]> {
  const params = new URLSearchParams({ business_id: businessId });
  if (statusFilter) params.set("status_filter", statusFilter);

  const response = await fetch(`${API_BASE_URL}/v1/escalations?${params}`);
  if (!response.ok) {
    throw new ApiError(await parseErrorDetail(response));
  }
  return response.json();
}

export async function resolveEscalation(
  escalationId: string,
  businessId: string,
): Promise<EscalationQueueItem> {
  const params = new URLSearchParams({ business_id: businessId });
  const response = await fetch(
    `${API_BASE_URL}/v1/escalations/${escalationId}/resolve?${params}`,
    { method: "POST" },
  );
  if (!response.ok) {
    throw new ApiError(await parseErrorDetail(response));
  }
  return response.json();
}
