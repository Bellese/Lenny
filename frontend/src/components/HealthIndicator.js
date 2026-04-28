import React, { useState } from 'react';
import styles from './HealthIndicator.module.css';

export default function HealthIndicator({ status, name, errorDetails }) {
  const [popoverOpen, setPopoverOpen] = useState(false);
  const ok = status === 'connected' || status === 'healthy';
  const displayName = name || 'Local CDR';

  return (
    <div
      className={`${styles.chip} ${ok ? '' : styles.chipErr}`}
      onMouseEnter={() => !ok && setPopoverOpen(true)}
      onMouseLeave={() => setPopoverOpen(false)}
      onFocus={() => !ok && setPopoverOpen(true)}
      onBlur={() => setPopoverOpen(false)}
      tabIndex={ok ? -1 : 0}
      aria-label={ok ? displayName : `${displayName} — connection error`}
    >
      <span className={`${styles.dot} ${ok ? styles.dotOk : styles.dotErr}`} />
      {displayName}
      {!ok && popoverOpen && (
        <div className={styles.popover} role="tooltip">
          {errorDetails?.hint && <p className={styles.popoverHint}>{errorDetails.hint}</p>}
          {errorDetails?.url && <p className={styles.popoverUrl}>{errorDetails.url}</p>}
          {!errorDetails?.hint && <p className={styles.popoverHint}>CDR connection failed.</p>}
        </div>
      )}
    </div>
  );
}
