import React, { useState, useEffect, useRef } from 'react';
import HealthIndicator from './HealthIndicator';
import styles from './HealthChipGroup.module.css';

const KIND_LABEL = { cdr: 'CDR', mcs: 'Measure Engine' };
const STATE_LABEL = {
  pending: 'checking',
  healthy: 'connected',
  unreachable: 'unreachable',
  none: 'no active connection',
};

function aggregateState(chipEntries) {
  if (chipEntries.some(([, c]) => c.state === 'unreachable')) return 'unreachable';
  if (chipEntries.every(([, c]) => c.state === 'healthy')) return 'healthy';
  return 'pending';
}

export default function HealthChipGroup({ chips, kinds, onChipClick }) {
  // kinds: array of { kind, settingsHash } in render order.
  const chipEntries = kinds.map(({ kind }) => [kind, chips[kind] || { state: 'pending', name: '', errorDetails: null }]);
  const agg = aggregateState(chipEntries);
  const [popoverOpen, setPopoverOpen] = useState(false);
  const aggRef = useRef(null);

  useEffect(() => {
    if (!popoverOpen) return undefined;
    const onDocClick = (e) => {
      if (aggRef.current && !aggRef.current.contains(e.target)) setPopoverOpen(false);
    };
    const onKey = (e) => { if (e.key === 'Escape') setPopoverOpen(false); };
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [popoverOpen]);

  const dotClass =
    agg === 'healthy' ? styles.dotOk
    : agg === 'unreachable' ? styles.dotErr
    : styles.dotPending;

  const aggLabel = `Connections: ${STATE_LABEL[agg]}`;

  return (
    <div className={styles.group}>
      {/* Wide layout — individual chips */}
      <div className={styles.chipsWide}>
        {kinds.map(({ kind, settingsHash }) => {
          const chip = chips[kind] || { state: 'pending', name: '', errorDetails: null };
          return (
            <HealthIndicator
              key={kind}
              kind={kind}
              state={chip.state}
              name={chip.name}
              errorDetails={chip.errorDetails}
              onClick={() => onChipClick(settingsHash)}
            />
          );
        })}
      </div>

      {/* Mobile layout — aggregate pill */}
      <div className={styles.chipsMobile} ref={aggRef}>
        <button
          type="button"
          className={`${styles.aggregate} ${agg === 'unreachable' ? styles.aggregateErr : ''}`}
          onClick={() => setPopoverOpen((o) => !o)}
          aria-haspopup="true"
          aria-expanded={popoverOpen}
          aria-label={aggLabel}
        >
          <span className={`${styles.dot} ${dotClass}`} />
          <span className={styles.aggregateLabel}>Connections</span>
        </button>

        {popoverOpen && (
          <div className={styles.popover} role="dialog" aria-label="Connection status">
            {kinds.map(({ kind, settingsHash }) => {
              const chip = chips[kind] || { state: 'pending', name: '', errorDetails: null };
              const rowDot =
                chip.state === 'healthy' ? styles.dotOk
                : chip.state === 'unreachable' ? styles.dotErr
                : styles.dotPending;
              return (
                <button
                  key={kind}
                  type="button"
                  className={styles.popoverRow}
                  onClick={() => { setPopoverOpen(false); onChipClick(settingsHash); }}
                >
                  <span className={`${styles.dot} ${rowDot}`} />
                  <span className={styles.popoverRowLabel}>{KIND_LABEL[kind]}</span>
                  <span className={styles.popoverRowStatus}>{STATE_LABEL[chip.state]}</span>
                </button>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
