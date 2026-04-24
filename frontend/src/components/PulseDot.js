import React from 'react';
import styles from './PulseDot.module.css';

export default function PulseDot({ color = 'var(--accent)' }) {
  return (
    <span className={styles.root} aria-hidden="true">
      <span className={styles.inner} style={{ background: color }} />
      <span className={styles.ring} style={{ background: color }} />
    </span>
  );
}
