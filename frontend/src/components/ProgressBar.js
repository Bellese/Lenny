import React from 'react';
import styles from './ProgressBar.module.css';

export default function ProgressBar({ value = 0, max = 100, label, size = 'md', variant = 'accent' }) {
  const percentage = max > 0 ? Math.min(100, Math.round((value / max) * 100)) : 0;

  return (
    <div className={styles.wrapper}>
      {label && (
        <div className={styles.label}>
          <span>{label}</span>
          <span className={styles.percentage}>{percentage}%</span>
        </div>
      )}
      <div
        className={`${styles.track} ${styles[size]}`}
        role="progressbar"
        aria-valuenow={value}
        aria-valuemin={0}
        aria-valuemax={max}
        aria-label={label || `Progress: ${percentage}%`}
      >
        <div
          className={`${styles.fill} ${styles[variant]}`}
          style={{ width: `${percentage}%` }}
        />
      </div>
    </div>
  );
}
