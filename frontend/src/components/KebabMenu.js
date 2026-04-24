import React, { useState, useEffect, useRef } from 'react';
import styles from './KebabMenu.module.css';

export default function KebabMenu({ items }) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState({ top: 0, right: 0 });
  const ref = useRef(null);
  const triggerRef = useRef(null);

  useEffect(() => {
    if (!open || !triggerRef.current) return;
    const rect = triggerRef.current.getBoundingClientRect();
    setPos({
      top: rect.bottom + 4,
      right: window.innerWidth - rect.right,
    });
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const h = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    window.addEventListener('mousedown', h);
    return () => window.removeEventListener('mousedown', h);
  }, [open]);

  return (
    <div ref={ref} className={styles.root} onClick={(e) => e.stopPropagation()}>
      <button
        ref={triggerRef}
        className={styles.trigger}
        onClick={() => setOpen(!open)}
        aria-label="More actions"
        aria-expanded={open}
      >
        <svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor">
          <circle cx="3" cy="7" r="1.2" />
          <circle cx="7" cy="7" r="1.2" />
          <circle cx="11" cy="7" r="1.2" />
        </svg>
      </button>
      {open && (
        <div className={styles.popover} style={{ top: pos.top, right: pos.right }} role="menu">
          {items.map((item, i) => {
            if (item.divider) return <div key={i} className={styles.divider} />;
            const disabled = item.disabled;
            return (
              <button
                key={i}
                role="menuitem"
                disabled={disabled}
                className={`${styles.item} ${item.tone === 'destructive' ? styles.itemDestructive : ''} ${disabled ? styles.itemDisabled : ''}`}
                onClick={() => { if (!disabled) { setOpen(false); item.onClick(); } }}
              >
                {item.icon && <span className={styles.itemIcon}>{item.icon}</span>}
                <span className={styles.itemLabel}>{item.label}</span>
                {item.hint && <span className={styles.itemHint}>{item.hint}</span>}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
