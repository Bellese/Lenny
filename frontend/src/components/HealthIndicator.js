import React, { useState } from 'react';
import styles from './HealthIndicator.module.css';

const KIND_DEFAULTS = {
  cdr: { fallbackName: 'Local CDR', noneLabel: 'No CDR active', errorPrefix: 'CDR' },
  mcs: { fallbackName: 'Local Measure Engine', noneLabel: 'No MCS active', errorPrefix: 'Measure Engine' },
};

// Four-state chip: pending | healthy | unreachable | none-active.
// state: 'pending' | 'healthy' | 'unreachable' | 'none'
export default function HealthIndicator({
  kind = 'cdr',
  state = 'pending',
  name,
  errorDetails,
  onClick,
}) {
  const [popoverOpen, setPopoverOpen] = useState(false);
  const cfg = KIND_DEFAULTS[kind] || KIND_DEFAULTS.cdr;

  const dotClass =
    state === 'healthy' ? styles.dotOk
    : state === 'unreachable' ? styles.dotErr
    : state === 'none' ? styles.dotNone
    : styles.dotPending;

  const showsPopover = state === 'unreachable' && popoverOpen;
  const isInteractive = state === 'unreachable' || state === 'none';
  const displayName =
    state === 'none' ? cfg.noneLabel
    : (name || cfg.fallbackName);

  const ariaLabel =
    state === 'pending' ? `${cfg.errorPrefix}: checking…`
    : state === 'unreachable' ? `${displayName} — connection error`
    : state === 'none' ? `${cfg.noneLabel} — click to configure`
    : displayName;

  const handleKeyDown = (e) => {
    if (!isInteractive || !onClick) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onClick();
    }
  };

  return (
    <div
      className={`${styles.chip} ${state === 'unreachable' ? styles.chipErr : ''} ${isInteractive && onClick ? styles.chipClickable : ''}`}
      onMouseEnter={() => state === 'unreachable' && setPopoverOpen(true)}
      onMouseLeave={() => setPopoverOpen(false)}
      onFocus={() => state === 'unreachable' && setPopoverOpen(true)}
      onBlur={() => setPopoverOpen(false)}
      onClick={isInteractive && onClick ? onClick : undefined}
      onKeyDown={handleKeyDown}
      tabIndex={isInteractive ? 0 : -1}
      role={isInteractive && onClick ? 'button' : undefined}
      aria-label={ariaLabel}
    >
      <span className={`${styles.dot} ${dotClass}`} />
      {displayName}
      {showsPopover && (
        <div className={styles.popover} role="tooltip">
          {errorDetails?.hint && <p className={styles.popoverHint}>{errorDetails.hint}</p>}
          {errorDetails?.url && <p className={styles.popoverUrl}>{errorDetails.url}</p>}
          {!errorDetails?.hint && <p className={styles.popoverHint}>{cfg.errorPrefix} connection failed.</p>}
        </div>
      )}
    </div>
  );
}
