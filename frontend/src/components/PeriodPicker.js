import React, { useState } from 'react';
import styles from './PeriodPicker.module.css';

export default function PeriodPicker({ periodStart, periodEnd, onChange, defaultYear }) {
  const currentYear = defaultYear || new Date().getFullYear();
  const years = Array.from({ length: 5 }, (_, i) => currentYear - 4 + i);

  const [customMode, setCustomMode] = useState(false);

  const selectedYear = periodStart ? Number(periodStart.slice(0, 4)) : currentYear;

  function handleYearChange(e) {
    const y = e.target.value;
    onChange(`${y}-01-01`, `${y}-12-31`);
  }

  function enterCustomMode() {
    setCustomMode(true);
  }

  function exitCustomMode() {
    setCustomMode(false);
    onChange(`${currentYear}-01-01`, `${currentYear}-12-31`);
  }

  if (customMode) {
    return (
      <div>
        <div className={styles.fieldRow}>
          <div className={styles.field}>
            <label className={styles.label} htmlFor="period-start">Period start</label>
            <input
              id="period-start"
              type="date"
              className={styles.input}
              value={periodStart}
              onChange={e => onChange(e.target.value, periodEnd)}
            />
          </div>
          <div className={styles.field}>
            <label className={styles.label} htmlFor="period-end">Period end</label>
            <input
              id="period-end"
              type="date"
              className={styles.input}
              value={periodEnd}
              onChange={e => onChange(periodStart, e.target.value)}
            />
          </div>
        </div>
        <button type="button" className={styles.toggleLink} onClick={exitCustomMode}>
          ← Back to year select
        </button>
      </div>
    );
  }

  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor="period-year">Reporting period</label>
      <select
        id="period-year"
        className={styles.select}
        value={selectedYear}
        onChange={handleYearChange}
      >
        {years.map(y => (
          <option key={y} value={y}>{y}</option>
        ))}
      </select>
      <button type="button" className={styles.toggleLink} onClick={enterCustomMode}>
        Enter custom dates →
      </button>
    </div>
  );
}
