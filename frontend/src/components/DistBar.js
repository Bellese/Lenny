import React from 'react';
import styles from './DistBar.module.css';

export default function DistBar({ ip, den, num, exc, height = 8 }) {
  if (!ip) return null;
  const numPct = (num / ip) * 100;
  const denNumPct = ((den - num) / ip) * 100;
  const excPct = (exc / ip) * 100;
  return (
    <div className={styles.bar} style={{ height }}>
      <div className={styles.segNum} style={{ width: `${numPct}%` }} />
      <div className={styles.segDen} style={{ width: `${denNumPct}%` }} />
      <div className={styles.segExc} style={{ width: `${excPct}%` }} />
    </div>
  );
}
