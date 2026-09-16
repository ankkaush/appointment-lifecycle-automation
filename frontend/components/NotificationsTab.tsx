"use client";

import { useEffect, useState } from "react";
import styles from "./dashboard.module.css";
import StatusBadge from "./StatusBadge";
import { ApiError, NotificationListItem, fetchNotifications } from "@/lib/api";
import { formatDateTime } from "@/lib/format";

export default function NotificationsTab({
  businessId,
  apiKey,
}: {
  businessId: string;
  apiKey: string;
}) {
  const [notifications, setNotifications] = useState<NotificationListItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- kicking off a fetch, not deriving from props/state
    setLoading(true);
    setError(null);
    fetchNotifications(businessId, apiKey)
      .then((result) => {
        if (!cancelled) setNotifications(result);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load notifications.");
        }
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
        <div className={styles.sectionTitle}>Notification activity</div>
        <p className={styles.sectionSubtitle}>
          Every confirmation, reminder, reschedule, cancellation, and no-show outreach the system
          has actually attempted to send.
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
                <th>Time</th>
                <th>Customer</th>
                <th>Event</th>
                <th>Channel</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {notifications.length === 0 && (
                <tr>
                  <td colSpan={5} className={styles.calendarEmpty}>
                    No notifications sent yet.
                  </td>
                </tr>
              )}
              {notifications.map((n) => (
                <tr key={n.id}>
                  <td>{formatDateTime(n.created_at)}</td>
                  <td>{n.customer_name}</td>
                  <td>{n.subject}</td>
                  <td>{n.channel}</td>
                  <td>
                    <StatusBadge status={n.status} />
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
