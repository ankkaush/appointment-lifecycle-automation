"use client";

import { useCallback, useEffect, useState } from "react";
import styles from "./dashboard.module.css";
import StatusBadge from "./StatusBadge";
import AppointmentDetailPanel from "./AppointmentDetailPanel";
import { ApiError, AppointmentListItem, fetchAppointments } from "@/lib/api";
import { formatDateTime, formatDayLabel, formatTime, isSameDay } from "@/lib/format";

function startOfWeek(date: Date): Date {
  const d = new Date(date);
  const day = d.getDay(); // 0 = Sunday
  const diff = day === 0 ? -6 : 1 - day; // Monday start
  d.setDate(d.getDate() + diff);
  d.setHours(0, 0, 0, 0);
  return d;
}

function addDays(date: Date, days: number): Date {
  const d = new Date(date);
  d.setDate(d.getDate() + days);
  return d;
}

export default function AppointmentsTab({
  businessId,
  apiKey,
}: {
  businessId: string;
  apiKey: string;
}) {
  const [weekStart, setWeekStart] = useState<Date>(() => startOfWeek(new Date()));
  const [appointments, setAppointments] = useState<AppointmentListItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedAppointmentId, setSelectedAppointmentId] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    fetchAppointments(businessId, apiKey, {
      windowStart: weekStart.toISOString(),
      windowEnd: addDays(weekStart, 7).toISOString(),
    })
      .then(setAppointments)
      .catch((err) => {
        setError(err instanceof ApiError ? err.message : "Could not load appointments.");
      })
      .finally(() => setLoading(false));
  }, [businessId, apiKey, weekStart]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- syncing with the backend whenever the visible week changes, the standard shape for this
    load();
  }, [load]);

  const days = Array.from({ length: 7 }, (_, i) => addDays(weekStart, i));
  const today = new Date();

  return (
    <div className={styles.section}>
      <div>
        <div className={styles.sectionTitle}>Appointments</div>
        <span className={styles.calendarLabel}>Appointment Calendar &mdash; Demo / Mock Calendar</span>
      </div>

      <div className={styles.calendarNav}>
        <button type="button" onClick={() => setWeekStart((d) => addDays(d, -7))}>
          &larr; Previous week
        </button>
        <span className={styles.calendarNavLabel}>
          {formatDayLabel(days[0])} &ndash; {formatDayLabel(days[6])}
        </span>
        <button type="button" onClick={() => setWeekStart((d) => addDays(d, 7))}>
          Next week &rarr;
        </button>
        <button type="button" onClick={() => setWeekStart(startOfWeek(new Date()))}>
          Today
        </button>
      </div>

      {error && <p className={styles.errorText}>{error}</p>}

      {loading ? (
        <p className={styles.sectionSubtitle}>Loading&hellip;</p>
      ) : (
        <div className={styles.calendarWeek}>
          {days.map((day) => {
            const dayAppointments = appointments
              .filter((a) => isSameDay(new Date(a.start_at), day))
              .sort((a, b) => a.start_at.localeCompare(b.start_at));
            return (
              <div
                key={day.toISOString()}
                className={`${styles.calendarDay} ${isSameDay(day, today) ? styles.calendarDayToday : ""}`}
              >
                <div className={styles.calendarDayHeader}>{formatDayLabel(day)}</div>
                {dayAppointments.length === 0 ? (
                  <span className={styles.calendarEmpty}>&mdash;</span>
                ) : (
                  dayAppointments.map((a) => (
                    <div
                      key={a.id}
                      className={styles.calendarSlot}
                      onClick={() => setSelectedAppointmentId(a.id)}
                    >
                      <span className={styles.calendarSlotTime}>{formatTime(a.start_at)}</span>
                      <span>
                        {a.customer_name} &mdash; {a.service_name}
                      </span>
                      <StatusBadge status={a.status} />
                    </div>
                  ))
                )}
              </div>
            );
          })}
        </div>
      )}

      <div>
        <div className={styles.sectionTitle}>All appointments this week</div>
        <div className={styles.tableWrap}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>When</th>
                <th>Customer</th>
                <th>Service</th>
                <th>Staff</th>
                <th>Status</th>
                <th>Calendar</th>
              </tr>
            </thead>
            <tbody>
              {appointments.length === 0 && (
                <tr>
                  <td colSpan={6} className={styles.calendarEmpty}>
                    No appointments in this window.
                  </td>
                </tr>
              )}
              {appointments.map((a) => (
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
                  <td>
                    <StatusBadge status={a.calendar_sync_status} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
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
