// Thin, typed wrapper around the backend's business-scoped endpoints.
// Every call sends the business's API key as a Bearer token -- the one
// place that concern lives, so no call site elsewhere needs to know.

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8010";

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

async function getJSON<T>(
  path: string,
  apiKey: string,
  params: Record<string, string | undefined>,
): Promise<T> {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) search.set(key, value);
  }
  const response = await fetch(`${API_BASE_URL}${path}?${search}`, {
    headers: authHeaders(apiKey),
  });
  if (!response.ok) {
    throw new ApiError(await parseErrorDetail(response));
  }
  return response.json();
}

// --- Escalations -------------------------------------------------------

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

export async function fetchEscalations(
  businessId: string,
  apiKey: string,
  statusFilter?: EscalationStatus,
): Promise<EscalationQueueItem[]> {
  return getJSON("/v1/escalations", apiKey, {
    business_id: businessId,
    status_filter: statusFilter,
  });
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

// --- Overview ------------------------------------------------------------

export interface OverviewSummary {
  upcoming_appointments: number;
  todays_appointments: number;
  pending_reminders: number;
  no_shows: number;
  open_escalations: number;
  notifications_sent: number;
  notifications_failed: number;
}

export async function fetchOverview(businessId: string, apiKey: string): Promise<OverviewSummary> {
  return getJSON("/v1/overview", apiKey, { business_id: businessId });
}

export interface ActivityEntry {
  at: string;
  label: string;
  detail: string | null;
  source: string;
  customer_name: string | null;
  appointment_id: string | null;
}

export async function fetchActivity(
  businessId: string,
  apiKey: string,
  limit = 20,
): Promise<ActivityEntry[]> {
  return getJSON("/v1/activity", apiKey, { business_id: businessId, limit: String(limit) });
}

// --- Appointments ----------------------------------------------------------

export type AppointmentStatus = "BOOKED" | "COMPLETED" | "CANCELLED" | "NO_SHOW";

export interface AppointmentListItem {
  id: string;
  status: AppointmentStatus;
  start_at: string;
  end_at: string;
  calendar_sync_status: string;
  customer_id: string;
  customer_name: string;
  customer_contact: string;
  service_name: string;
  staff_name: string;
  rebooked_from_id: string | null;
}

export async function fetchAppointments(
  businessId: string,
  apiKey: string,
  options?: { statusFilter?: AppointmentStatus; windowStart?: string; windowEnd?: string },
): Promise<AppointmentListItem[]> {
  return getJSON("/v1/appointments", apiKey, {
    business_id: businessId,
    status_filter: options?.statusFilter,
    window_start: options?.windowStart,
    window_end: options?.windowEnd,
  });
}

export interface TimelineEntry {
  at: string;
  label: string;
  detail: string | null;
  source: string;
}

export interface AppointmentTimeline {
  appointment: AppointmentListItem;
  entries: TimelineEntry[];
}

export async function fetchAppointmentTimeline(
  appointmentId: string,
  businessId: string,
  apiKey: string,
): Promise<AppointmentTimeline> {
  return getJSON(`/v1/appointments/${appointmentId}/timeline`, apiKey, {
    business_id: businessId,
  });
}

// --- Customers ---------------------------------------------------------

export interface CustomerListItem {
  id: string;
  name: string;
  contact: string;
  total_appointments: number;
  upcoming_appointment_at: string | null;
  latest_status: string | null;
}

export async function fetchCustomers(
  businessId: string,
  apiKey: string,
): Promise<CustomerListItem[]> {
  return getJSON("/v1/customers", apiKey, { business_id: businessId });
}

// --- Notifications -------------------------------------------------------

export interface NotificationListItem {
  id: string;
  created_at: string;
  customer_name: string;
  appointment_id: string | null;
  subject: string;
  channel: string;
  status: "SENT" | "FAILED";
}

export async function fetchNotifications(
  businessId: string,
  apiKey: string,
): Promise<NotificationListItem[]> {
  return getJSON("/v1/notifications", apiKey, { business_id: businessId });
}

// --- Background jobs -----------------------------------------------------

export interface ScheduledJobListItem {
  id: string;
  job_type: string;
  status: "PENDING" | "LOCKED" | "DONE" | "FAILED_RETRYABLE";
  run_at: string;
  attempts: number;
  last_error: string | null;
  customer_name: string | null;
  appointment_id: string | null;
}

export async function fetchJobs(
  businessId: string,
  apiKey: string,
): Promise<ScheduledJobListItem[]> {
  return getJSON("/v1/jobs", apiKey, { business_id: businessId });
}
