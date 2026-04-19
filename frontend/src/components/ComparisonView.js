import React, { useState, useEffect } from 'react';
import styles from './ComparisonView.module.css';
import { getJobComparison } from '../api/client';

function MatchIcon({ match }) {
  return match
    ? <span className={styles.matchIcon} aria-label="Match" title="Match">&#10003;</span>
    : <span className={styles.mismatchIcon} aria-label="Mismatch" title="Mismatch">&#9888;</span>;
}

function PopCount({ code, expected, actual }) {
  const match = expected === actual;
  return (
    <td className={match ? styles.countMatch : styles.countMismatch} title={`Expected: ${expected}, Actual: ${actual}`}>
      <span className={styles.countVal}>{actual ?? 0}</span>
      {!match && <span className={styles.countExpected}>(exp: {expected ?? 0})</span>}
    </td>
  );
}

const POPULATION_CODES = [
  'initial-population',
  'denominator',
  'denominator-exclusion',
  'numerator',
  'numerator-exclusion',
];

const POPULATION_LABELS = {
  'initial-population': 'Initial Pop',
  'denominator': 'Denom',
  'denominator-exclusion': 'Denom Excl',
  'numerator': 'Numer',
  'numerator-exclusion': 'Numer Excl',
};

export default function ComparisonView({ jobId }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!jobId) return;
    setLoading(true);
    setError(null);
    getJobComparison(jobId)
      .then(setData)
      .catch(err => setError(err.message || 'Failed to load comparison'))
      .finally(() => setLoading(false));
  }, [jobId]);

  if (loading) return <div className={styles.loading}>Loading comparison...</div>;
  if (error) return <div className={styles.error}>Comparison unavailable: {error}</div>;
  if (!data || !data.has_expected) {
    return (
      <div className={styles.noExpected}>
        No expected results available for this measure and period.
        Load a connectathon bundle via Settings to enable comparison.
      </div>
    );
  }

  const { matched, total, patients } = data;
  const allMatch = matched === total;

  return (
    <div className={styles.container}>
      <div className={styles.summary}>
        <span className={styles.summaryLabel}>Expected vs Actual</span>
        <span className={allMatch ? styles.summaryPass : styles.summaryFail}>
          {matched} / {total} patients match expected results
        </span>
      </div>

      {total > 50 && (
        <div className={styles.truncationWarning}>
          Showing first 50 of {total} patients.
        </div>
      )}

      <div className={styles.tableWrapper}>
        <table className={styles.table} aria-label="Expected vs Actual Comparison">
          <thead>
            <tr>
              <th>Patient</th>
              <th>Status</th>
              {POPULATION_CODES.map(code => (
                <th key={code}>{POPULATION_LABELS[code]}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {patients.slice(0, 50).map((p, i) => (
              <tr key={p.subject_reference || i} className={p.match ? styles.rowMatch : styles.rowMismatch}>
                <td className={styles.patientRef}>{p.subject_reference}</td>
                <td className={styles.statusCell}><MatchIcon match={p.match} /></td>
                {POPULATION_CODES.map(code => (
                  <PopCount
                    key={code}
                    code={code}
                    expected={p.expected?.[code] ?? 0}
                    actual={p.actual?.[code] ?? 0}
                  />
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
