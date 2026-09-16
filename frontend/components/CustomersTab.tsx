"use client";

import { useEffect, useState } from "react";
import styles from "./dashboard.module.css";
import StatusBadge from "./StatusBadge";
import { ApiError, CustomerListItem, fetchCustomers } from "@/lib/api";
import { formatDateTime } from "@/lib/format";

export default function CustomersTab({
  businessId,
  apiKey,
}: {
  businessId: string;
  apiKey: string;
}) {
  const [customers, setCustomers] = useState<CustomerListItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- kicking off a fetch, not deriving from props/state
    setLoading(true);
    setError(null);
    fetchCustomers(businessId, apiKey)
      .then((result) => {
        if (!cancelled) setCustomers(result);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : "Could not load customers.");
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
        <div className={styles.sectionTitle}>Customers</div>
        <p className={styles.sectionSubtitle}>Everyone who has messaged this business.</p>
      </div>

      {error && <p className={styles.errorText}>{error}</p>}
      {loading ? (
        <p className={styles.sectionSubtitle}>Loading&hellip;</p>
      ) : (
        <div className={styles.tableWrap}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Name</th>
                <th>Contact</th>
                <th>Total appointments</th>
                <th>Upcoming</th>
                <th>Latest status</th>
              </tr>
            </thead>
            <tbody>
              {customers.length === 0 && (
                <tr>
                  <td colSpan={5} className={styles.calendarEmpty}>
                    No customers yet.
                  </td>
                </tr>
              )}
              {customers.map((c) => (
                <tr key={c.id}>
                  <td>{c.name}</td>
                  <td>{c.contact}</td>
                  <td>{c.total_appointments}</td>
                  <td>{c.upcoming_appointment_at ? formatDateTime(c.upcoming_appointment_at) : "—"}</td>
                  <td>{c.latest_status ? <StatusBadge status={c.latest_status} /> : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
