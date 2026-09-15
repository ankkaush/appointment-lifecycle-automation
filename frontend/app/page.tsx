"use client";

import { useCallback, useEffect, useState } from "react";
import styles from "./page.module.css";
import {
  ApiError,
  EscalationQueueItem,
  EscalationStatus,
  fetchEscalations,
  resolveEscalation,
} from "@/lib/api";

const BUSINESS_ID_STORAGE_KEY = "dashboard.businessId";

function formatTimestamp(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

export default function DashboardPage() {
  const [businessId, setBusinessId] = useState("");
  const [statusFilter, setStatusFilter] = useState<EscalationStatus | "ALL">("OPEN");
  const [escalations, setEscalations] = useState<EscalationQueueItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resolvingId, setResolvingId] = useState<string | null>(null);

  useEffect(() => {
    // localStorage isn't available during server rendering, so the
    // stored business id has to be picked up client-side after mount
    // rather than as a useState initializer.
    const stored = window.localStorage.getItem(BUSINESS_ID_STORAGE_KEY);
    // eslint-disable-next-line react-hooks/set-state-in-effect -- syncing from an external, client-only store, not a derived render value
    if (stored) setBusinessId(stored);
  }, []);

  const load = useCallback(async (id: string, filter: EscalationStatus | "ALL") => {
    if (!id.trim()) {
      setEscalations([]);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const items = await fetchEscalations(id.trim(), filter === "ALL" ? undefined : filter);
      setEscalations(items);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the API.");
      setEscalations([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!businessId.trim()) return;
    window.localStorage.setItem(BUSINESS_ID_STORAGE_KEY, businessId.trim());
    // Syncing with the backend (an external system) whenever these two
    // inputs change -- the standard shape for this, per React's own
    // guidance, even though `load`'s eventual setState calls happen
    // asynchronously after the fetch resolves, not synchronously here.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load(businessId, statusFilter);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only re-fetch on these two, not on `load` identity
  }, [businessId, statusFilter]);

  async function handleResolve(item: EscalationQueueItem) {
    setResolvingId(item.id);
    setError(null);
    try {
      await resolveEscalation(item.id, businessId.trim());
      await load(businessId, statusFilter);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not resolve this escalation.");
    } finally {
      setResolvingId(null);
    }
  }

  return (
    <main className={styles.page}>
      <div className={styles.header}>
        <h1 className={styles.title}>Escalation Queue</h1>
        <p className={styles.subtitle}>
          Requests the automated workflow couldn&apos;t resolve on its own.
        </p>
      </div>

      <div className={styles.controls}>
        <div className={styles.field}>
          <label htmlFor="business-id">Business ID</label>
          <input
            id="business-id"
            type="text"
            placeholder="00000000-0000-0000-0000-000000000000"
            value={businessId}
            onChange={(e) => setBusinessId(e.target.value)}
          />
        </div>
        <div className={styles.field}>
          <label htmlFor="status-filter">Status</label>
          <select
            id="status-filter"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value as EscalationStatus | "ALL")}
          >
            <option value="OPEN">Open</option>
            <option value="RESOLVED">Resolved</option>
            <option value="ALL">All</option>
          </select>
        </div>
        <button
          type="button"
          className={styles.refreshButton}
          disabled={loading || !businessId.trim()}
          onClick={() => load(businessId, statusFilter)}
        >
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>

      {!businessId.trim() && (
        <p className={styles.status}>Enter a business ID to load its escalation queue.</p>
      )}
      {error && <p className={styles.error}>{error}</p>}

      {businessId.trim() && !error && (
        <div className={styles.list}>
          {escalations.length === 0 && !loading && (
            <p className={styles.empty}>No escalations to show.</p>
          )}
          {escalations.map((item) => (
            <div key={item.id} className={styles.card}>
              <div className={styles.cardTop}>
                <span className={styles.reason}>{item.reason}</span>
                <span className={styles.timestamp}>{formatTimestamp(item.created_at)}</span>
              </div>
              <div className={styles.customer}>
                {item.customer_name} &middot; {item.customer_contact}
              </div>
              <div className={styles.message}>&ldquo;{item.raw_message}&rdquo;</div>
              <div className={styles.cardBottom}>
                <span
                  className={`${styles.badge} ${
                    item.status === "OPEN" ? styles.badgeOpen : styles.badgeResolved
                  }`}
                >
                  {item.status}
                </span>
                {item.status === "OPEN" && (
                  <button
                    type="button"
                    className={styles.resolveButton}
                    disabled={resolvingId === item.id}
                    onClick={() => handleResolve(item)}
                  >
                    {resolvingId === item.id ? "Resolving…" : "Resolve"}
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </main>
  );
}
