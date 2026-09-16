"use client";

import { useEffect, useState } from "react";
import styles from "./dashboard.module.css";
import StatusBadge from "./StatusBadge";
import TimelineList from "./TimelineList";
import AppointmentDetailPanel from "./AppointmentDetailPanel";
import {
  ActivityEntry,
  ApiError,
  AppointmentListItem,
  OverviewSummary,
  fetchActivity,
  fetchAppointments,
  fetchOverview,
} from "@/lib/api";
import { formatDateTime } from "@/lib/format";

const CARD_DEFS: { key: keyof OverviewSummary; label: string; warn?: boolean }[] = [
  { key: "upcoming_appointments", label: "Upcoming appointments" },
  { key: "todays_appointments", label: "Today" },
  { key: "pending_reminders", label: "Pending reminders" },
  { key: "no_shows", label: "No-shows", warn: true },
  { key: "open_escalations", label: "Open escalations", warn: true },
  { key: "notifications_sent", label: "Notifications sent" },
  { key: "notifications_failed", label: "Notifications failed", warn: true },
];

export default function OverviewTab({
  businessId,
  apiKey,
}: {
  businessId: string;
  apiKey: string;
}) {
  const [summary, setSummary] = useState<OverviewSummary | null>(null);
  const [upcoming, setUpcoming] = useState<AppointmentListItem[]>([]);
  const [activity, setActivity] = useState<ActivityEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedAppointmentId, setSelectedAppointmentId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- kicking off a fetch, not deriving from props/state
    setLoading(true);
    setError(null);
    Promise.all([
      fetchOverview(businessId, apiKey),
      fetchAppointments(businessId, apiKey, { statusFilter: "BOOKED" }),
      fetchActivity(businessId, apiKey, 12),
    ])
      .then(([s, appointments, act]) => {
        if (cancelled) return;
        setSummary(s);
        setUpcoming(
          appointments
            .filter((a) => new Date(a.start_at) > new Date())
            .sort((a, b) => a.start_at.localeCompare(b.start_at))
            .slice(0, 6),
        );
        setActivity(act);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load the overview.");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [businessId, apiKey]);

  if (loading) return <p className={styles.sectionSubtitle}>Loading overview&hellip;</p>;
  if (error) return <p className={styles.errorText}>{error}</p>;
  if (!summary) return null;

  return (
    <div className={styles.section}>
      <div>
        <div className={styles.sectionTitle}>What the automation is doing</div>
        <p className={styles.sectionSubtitle}>
          Live counts from the database -- an AI-assisted appointment lifecycle running end to
          end: intake, booking, reminders, no-show recovery, and human escalation when it
          can&apos;t proceed safely.
        </p>
      </div>

      <div className={styles.cardGrid}>
        {CARD_DEFS.map((def) => (
          <div
            key={def.key}
            className={`${styles.statCard} ${def.warn && summary[def.key] > 0 ? styles.statCardWarn : ""}`}
          >
            <span className={styles.statValue}>{summary[def.key]}</span>
            <span className={styles.statLabel}>{def.label}</span>
          </div>
        ))}
      </div>

      <div>
        <div className={styles.sectionTitle}>Upcoming appointments</div>
        {upcoming.length === 0 ? (
          <p className={styles.calendarEmpty}>Nothing booked yet.</p>
        ) : (
          <div className={styles.tableWrap}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>When</th>
                  <th>Customer</th>
                  <th>Service</th>
                  <th>Staff</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {upcoming.map((a) => (
                  <tr
                    key={a.id}
                    className={styles.clickableRow}
                    onClick={() => setSelectedAppointmentId(a.id)}
                  >
                    <td>{formatDateTime(a.start_at)}</td>
                    <td>{a.customer_name}</td>
                    <td>{a.service_name}</td>
                    <td>{a.staff_name}</td>
                    <td>
                      <StatusBadge status={a.status} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div>
        <div className={styles.sectionTitle}>Recent automation activity</div>
        <TimelineList
          entries={activity.map((e) => ({
            at: e.at,
            label: e.customer_name ? `${e.customer_name} — ${e.label}` : e.label,
            detail: e.detail,
          }))}
          emptyMessage="No activity recorded yet."
        />
      </div>

      {selectedAppointmentId && (
        <AppointmentDetailPanel
          appointmentId={selectedAppointmentId}
          businessId={businessId}
          apiKey={apiKey}
          onClose={() => setSelectedAppointmentId(null)}
        />
      )}
    </div>
  );
}
