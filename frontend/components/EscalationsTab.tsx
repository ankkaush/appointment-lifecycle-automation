"use client";

import { useCallback, useEffect, useState } from "react";
import styles from "./dashboard.module.css";
import StatusBadge from "./StatusBadge";
import {
  ApiError,
  EscalationQueueItem,
  EscalationStatus,
  fetchEscalations,
  resolveEscalation,
} from "@/lib/api";
import { formatDateTime } from "@/lib/format";

export default function EscalationsTab({
  businessId,
  apiKey,
}: {
  businessId: string;
  apiKey: string;
}) {
  const [statusFilter, setStatusFilter] = useState<EscalationStatus | "ALL">("OPEN");
  const [escalations, setEscalations] = useState<EscalationQueueItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [resolvingId, setResolvingId] = useState<string | null>(null);

  const load = useCallback(
    async (filter: EscalationStatus | "ALL") => {
      setLoading(true);
      setError(null);
      try {
        const items = await fetchEscalations(businessId, apiKey, filter === "ALL" ? undefined : filter);
        setEscalations(items);
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Could not reach the API.");
        setEscalations([]);
      } finally {
        setLoading(false);
      }
    },
    [businessId, apiKey],
  );

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- syncing with the backend whenever the status filter changes, the standard shape for this
    load(statusFilter);
  }, [load, statusFilter]);

  async function handleResolve(item: EscalationQueueItem) {
    setResolvingId(item.id);
    setError(null);
    try {
      await resolveEscalation(item.id, businessId, apiKey);
      await load(statusFilter);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not resolve this escalation.");
    } finally {
      setResolvingId(null);
    }
  }

  return (
    <div className={styles.section}>
      <div>
        <div className={styles.sectionTitle}>Escalation queue</div>
        <p className={styles.sectionSubtitle}>
          Requests the automated workflow couldn&apos;t resolve on its own -- the human-in-the-loop
          part of this system.
        </p>
      </div>

      <div className={styles.calendarNav}>
        <label htmlFor="escalation-status-filter" className={styles.calendarNavLabel}>
          Status
        </label>
        <select
          id="escalation-status-filter"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value as EscalationStatus | "ALL")}
        >
          <option value="OPEN">Open</option>
          <option value="RESOLVED">Resolved</option>
          <option value="ALL">All</option>
        </select>
      </div>

      {error && <p className={styles.errorText}>{error}</p>}

      {loading ? (
        <p className={styles.sectionSubtitle}>Loading&hellip;</p>
      ) : (
        <div className={styles.section}>
          {escalations.length === 0 && <p className={styles.calendarEmpty}>No escalations to show.</p>}
          {escalations.map((item) => (
            <div key={item.id} className={styles.statCard}>
              <div className={styles.panelHeader}>
                <span className={styles.timelineLabel}>{item.reason}</span>
                <span className={styles.calendarNavLabel}>{formatDateTime(item.created_at)}</span>
              </div>
              <div className={styles.panelMeta}>
                {item.customer_name} &middot; {item.customer_contact}
              </div>
              <div className={styles.quoteBox}>&ldquo;{item.raw_message}&rdquo;</div>
              <div className={styles.panelHeader}>
                <StatusBadge status={item.status} />
                {item.status === "OPEN" && (
                  <button
                    type="button"
                    className={styles.panelClose}
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
    </div>
  );
}
