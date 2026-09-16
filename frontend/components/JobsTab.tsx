"use client";

import { useEffect, useState } from "react";
import styles from "./dashboard.module.css";
import StatusBadge from "./StatusBadge";
import { ApiError, ScheduledJobListItem, fetchJobs } from "@/lib/api";
import { formatDateTime } from "@/lib/format";

export default function JobsTab({ businessId, apiKey }: { businessId: string; apiKey: string }) {
  const [jobs, setJobs] = useState<ScheduledJobListItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- kicking off a fetch, not deriving from props/state
    setLoading(true);
    setError(null);
    fetchJobs(businessId, apiKey)
      .then((result) => {
        if (!cancelled) setJobs(result);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : "Could not load jobs.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [businessId, apiKey]);

  return (
    <div className={styles.section}>
      <div>
        <div className={styles.sectionTitle}>Background jobs</div>
        <p className={styles.sectionSubtitle}>
          The automation keeps working after the customer leaves the chat -- a background worker
          process polls for due reminders and sweeps for no-shows on its own timer, independent of
          any request. Only reminders are individually scheduled rows like these; no-show
          detection is a periodic sweep with no per-appointment row of its own (see that
          appointment&apos;s timeline instead).
        </p>
      </div>

      {error && <p className={styles.errorText}>{error}</p>}
      {loading ? (
        <p className={styles.sectionSubtitle}>Loading&hellip;</p>
      ) : (
        <div className={styles.tableWrap}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Job</th>
                <th>Customer</th>
                <th>Scheduled for</th>
                <th>Attempts</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {jobs.length === 0 && (
                <tr>
                  <td colSpan={5} className={styles.calendarEmpty}>
                    No scheduled jobs yet.
                  </td>
                </tr>
              )}
              {jobs.map((j) => (
                <tr key={j.id}>
                  <td>{j.job_type.replace("_", " ")}</td>
                  <td>{j.customer_name ?? "—"}</td>
                  <td>{formatDateTime(j.run_at)}</td>
                  <td>{j.attempts}</td>
                  <td>
                    <StatusBadge status={j.status} />
                    {j.last_error && <div className={styles.panelMeta}>{j.last_error}</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
