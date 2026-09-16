import styles from "./dashboard.module.css";

// One shared color mapping for every status string this dashboard shows
// (appointments, notifications, jobs, escalations) -- "good" (green),
// "bad" (red), or "in progress" (neutral blue), so the same visual
// language means the same thing everywhere in the UI.
const GOOD = new Set(["SENT", "SYNCED", "RESOLVED", "DONE", "BOOKED", "COMPLETED"]);
const BAD = new Set(["FAILED", "NO_SHOW", "CANCELLED", "FAILED_RETRYABLE"]);

export default function StatusBadge({ status }: { status: string }) {
  const tone = GOOD.has(status) ? styles.badgeGood : BAD.has(status) ? styles.badgeBad : styles.badgeNeutral;
  return <span className={`${styles.badge} ${tone}`}>{status}</span>;
}
