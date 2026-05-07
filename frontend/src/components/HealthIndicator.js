import React, { useState, useRef } from 'react';
import styles from './HealthIndicator.module.css';

const KIND_DEFAULTS = {
  cdr: { label: 'CDR', fallbackName: 'Local CDR', noneLabel: 'No CDR active' },
  mcs: { label: 'Measure Engine', fallbackName: 'Local Measure Engine', noneLabel: 'No MCS active' },
};

const STATE_LABEL = {
  pending: 'checking',
  healthy: 'connected',
  unreachable: 'unreachable',
  none: 'no active connection',
};

// Four-state chip: pending | healthy | unreachable | none.
export default function HealthIndicator({
  kind = 'cdr',
  state = 'pending',
  name,
  errorDetails,
  onClick,
}) {
  const [popoverOpen, setPopoverOpen] = useState(false);
  const ref = useRef(null);
  const cfg = KIND_DEFAULTS[kind] || KIND_DEFAULTS.cdr;

  const dotClass =
    state === 'healthy' ? styles.dotOk
    : state === 'unreachable' ? styles.dotErr
    : state === 'none' ? styles.dotNone
    : styles.dotPending;

  const hasPopover = state === 'unreachable';
  const showsPopover = hasPopover && popoverOpen;
  const displayName =
    state === 'none' ? cfg.noneLabel
    : (name || cfg.fallbackName);

  // ARIA pattern: "{kind}: {name}, {status}"
  const ariaLabel = `${cfg.label}: ${displayName}, ${STATE_LABEL[state] || state}`;

  const handleKeyDown = (e) => {
    if (e.key === 'Escape' && popoverOpen) {
      e.preventDefault();
      setPopoverOpen(false);
      return;
    }
    if (!onClick) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onClick();
    }
  };

  const handleClick = () => {
    if (onClick) onClick();
  };

  return (
    <div
      ref={ref}
      className={`${styles.chip} ${state === 'unreachable' ? styles.chipErr : ''} ${onClick ? styles.chipClickable : ''}`}
      onMouseEnter={() => hasPopover && setPopoverOpen(true)}
      onMouseLeave={() => setPopoverOpen(false)}
      onFocus={() => hasPopover && setPopoverOpen(true)}
      onBlur={() => setPopoverOpen(false)}
      onClick={onClick ? handleClick : undefined}
      onKeyDown={handleKeyDown}
      tabIndex={0}
      role={onClick ? 'button' : undefined}
      aria-label={ariaLabel}
    >
      <span className={`${styles.dot} ${dotClass}`} />
      {displayName}
      {showsPopover && (
        <div className={styles.popover} role="tooltip">
          {errorDetails?.hint && <p className={styles.popoverHint}>{errorDetails.hint}</p>}
          {errorDetails?.url && <p className={styles.popoverUrl}>{errorDetails.url}</p>}
          {!errorDetails?.hint && <p className={styles.popoverHint}>{cfg.label} connection failed.</p>}
        </div>
      )}
    </div>
  );
}
