"use client";

import { useEffect, useState } from "react";
import styles from "./dashboard.module.css";
import StatusBadge from "./StatusBadge";
import TimelineList from "./TimelineList";
import { ApiError, AppointmentTimeline, fetchAppointmentTimeline } from "@/lib/api";
import { formatDateTime } from "@/lib/format";

export default function AppointmentDetailPanel({
  appointmentId,
  businessId,
  apiKey,
  onClose,
}: {
  appointmentId: string;
  businessId: string;
  apiKey: string;
  onClose: () => void;
}) {
  const [data, setData] = useState<AppointmentTimeline | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- kicking off a fetch, not deriving from props/state
    setLoading(true);
    setError(null);
    fetchAppointmentTimeline(appointmentId, businessId, apiKey)
      .then((result) => {
        if (!cancelled) setData(result);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load this appointment.");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [appointmentId, businessId, apiKey]);

  return (
    <div className={styles.overlay} onClick={onClose}>
      <div className={styles.panel} onClick={(e) => e.stopPropagation()}>
        <div className={styles.panelHeader}>
          <div>
            <div className={styles.sectionTitle}>Appointment</div>
            {data && (
              <div className={styles.panelMeta}>
                {data.appointment.customer_name} &middot; {data.appointment.service_name} with{" "}
                {data.appointment.staff_name}
              </div>
            )}
          </div>
          <button type="button" className={styles.panelClose} onClick={onClose}>
            Close
          </button>
        </div>

        {loading && <p className={styles.panelMeta}>Loading&hellip;</p>}
        {error && <p className={styles.errorText}>{error}</p>}

        {data && (
          <>
            <div className={styles.badgeRow}>
              <StatusBadge status={data.appointment.status} />
              <span className={styles.panelMeta}>{formatDateTime(data.appointment.start_at)}</span>
              <StatusBadge status={data.appointment.calendar_sync_status} />
            </div>
            {data.appointment.rebooked_from_id && (
              <p className={styles.panelMeta}>
                Rebooked from a prior no-show appointment (original kept, immutable).
              </p>
            )}
            <div>
              <div className={styles.sectionTitle}>Lifecycle</div>
              <TimelineList entries={data.entries} emptyMessage="No recorded activity yet." />
            </div>
          </>
        )}
      </div>
    </div>
  );
}
