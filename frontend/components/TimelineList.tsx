import styles from "./dashboard.module.css";
import { formatDateTime } from "@/lib/format";

export interface GenericTimelineEntry {
  at: string;
  label: string;
  detail?: string | null;
}

export default function TimelineList({
  entries,
  emptyMessage,
}: {
  entries: GenericTimelineEntry[];
  emptyMessage: string;
}) {
  if (entries.length === 0) {
    return <p className={styles.calendarEmpty}>{emptyMessage}</p>;
  }
  return (
    <div className={styles.timeline}>
      {entries.map((entry, i) => (
        <div key={i} className={styles.timelineEntry}>
          <span className={styles.timelineTime}>{formatDateTime(entry.at)}</span>
          <div className={styles.timelineBody}>
            <span className={styles.timelineLabel}>{entry.label}</span>
            {entry.detail && <span className={styles.timelineDetail}>{entry.detail}</span>}
          </div>
        </div>
      ))}
    </div>
  );
}
