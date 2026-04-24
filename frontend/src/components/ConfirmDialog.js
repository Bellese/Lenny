import React, { useEffect } from 'react';
import styles from './ConfirmDialog.module.css';

export default function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel = 'Delete',
  cancelLabel = 'Cancel',
  tone = 'destructive',
  onConfirm,
  onCancel,
}) {
  useEffect(() => {
    if (!open) return;
    const h = (e) => { if (e.key === 'Escape') onCancel(); };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [open, onCancel]);

  if (!open) return null;

  return (
    <div className={styles.backdrop} onClick={onCancel} role="dialog" aria-modal="true" aria-label={title}>
      <div className={styles.panel} onClick={(e) => e.stopPropagation()}>
        <div className={`${styles.icon} ${tone === 'destructive' ? styles.iconDestructive : styles.iconNeutral}`}>
          <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
            <path d="M9 6v4M9 13v.01" />
            <path d="M9 2l7 12H2L9 2z" />
          </svg>
        </div>
        <div className={styles.title}>{title}</div>
        <div className={styles.body}>{body}</div>
        <div className={styles.actions}>
          <button type="button" className={styles.cancelBtn} onClick={onCancel}>{cancelLabel}</button>
          <button
            type="button"
            className={`${styles.confirmBtn} ${tone === 'destructive' ? styles.confirmDestructive : styles.confirmNeutral}`}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
