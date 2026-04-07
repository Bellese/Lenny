import React, { useState, useEffect, useCallback, useRef } from 'react';
import styles from './ValidationPage.module.css';
import {
  uploadTestBundle,
  getUploads,
  getExpectedResults,
  startValidationRun,
  getValidationRuns,
  getValidationRun,
} from '../api/client';

function SummaryCard({ label, value, variant = 'default' }) {
  return (
    <div className={`${styles.card} ${styles[variant]}`}>
      <span className={styles.cardLabel}>{label}</span>
      <span className={styles.cardCount}>{value !== undefined && value !== null ? value : '--'}</span>
    </div>
  );
}

function StatusBadge({ status }) {
  return <span className={`${styles.badge} ${styles[`badge_${status}`]}`}>{status}</span>;
}

export default function ValidationPage() {
  const [uploads, setUploads] = useState([]);
  const [expected, setExpected] = useState(null);
  const [runs, setRuns] = useState([]);
  const [selectedRunId, setSelectedRunId] = useState('');
  const [runDetail, setRunDetail] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [runStarting, setRunStarting] = useState(false);
  const [error, setError] = useState(null);
  const fileInputRef = useRef(null);
  const pollRef = useRef(null);

  // Load initial data
  const loadData = useCallback(async () => {
    try {
      const [uploadsData, expectedData, runsData] = await Promise.all([
        getUploads(),
        getExpectedResults(),
        getValidationRuns(),
      ]);
      setUploads(uploadsData.uploads || []);
      setExpected(expectedData);
      setRuns(runsData.runs || []);
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  // Poll for status updates when uploads or runs are in progress
  useEffect(() => {
    const hasActive = uploads.some(u => u.status === 'queued' || u.status === 'running')
      || runs.some(r => r.status === 'queued' || r.status === 'running');

    if (hasActive) {
      pollRef.current = setInterval(loadData, 3000);
    } else {
      clearInterval(pollRef.current);
    }
    return () => clearInterval(pollRef.current);
  }, [uploads, runs, loadData]);

  // Load run detail when selected
  useEffect(() => {
    if (!selectedRunId) {
      setRunDetail(null);
      return;
    }
    async function loadDetail() {
      try {
        const detail = await getValidationRun(selectedRunId);
        setRunDetail(detail);
      } catch (err) {
        setError(err.message);
      }
    }
    loadDetail();
  }, [selectedRunId]);

  // Auto-select latest completed run
  useEffect(() => {
    if (!selectedRunId && runs.length > 0) {
      const completed = runs.find(r => r.status === 'complete');
      if (completed) setSelectedRunId(String(completed.id));
    }
  }, [runs, selectedRunId]);

  const handleUpload = async () => {
    const file = fileInputRef.current?.files[0];
    if (!file) return;
    setUploading(true);
    setError(null);
    try {
      await uploadTestBundle(file);
      fileInputRef.current.value = '';
      await loadData();
    } catch (err) {
      setError(err.message);
    } finally {
      setUploading(false);
    }
  };

  const handleRunValidation = async () => {
    setRunStarting(true);
    setError(null);
    try {
      const result = await startValidationRun({});
      setSelectedRunId(String(result.id));
      await loadData();
    } catch (err) {
      setError(err.message);
    } finally {
      setRunStarting(false);
    }
  };

  const hasExpected = expected && expected.total_measures > 0;

  return (
    <div className={styles.page}>
      <h1 className={styles.title}>Measure Validation</h1>

      {error && (
        <div className={styles.errorBanner} role="alert">
          {error}
          <button onClick={() => setError(null)} className={styles.dismissBtn}>Dismiss</button>
        </div>
      )}

      {/* Upload Section */}
      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>Upload Test Bundle</h2>
        <div className={styles.uploadRow}>
          <input
            ref={fileInputRef}
            type="file"
            accept=".json"
            className={styles.fileInput}
          />
          <button
            onClick={handleUpload}
            disabled={uploading}
            className={styles.primaryBtn}
          >
            {uploading ? 'Uploading...' : 'Upload Bundle'}
          </button>
        </div>

        {uploads.length > 0 && (
          <div className={styles.uploadHistory}>
            <h3 className={styles.subTitle}>Upload History</h3>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>File</th>
                  <th>Status</th>
                  <th>Measures</th>
                  <th>Patients</th>
                  <th>Expected Results</th>
                  <th>Uploaded</th>
                </tr>
              </thead>
              <tbody>
                {uploads.map(u => (
                  <tr key={u.id}>
                    <td>{u.filename}</td>
                    <td><StatusBadge status={u.status} /></td>
                    <td>{u.measures_loaded}</td>
                    <td>{u.patients_loaded}</td>
                    <td>{u.expected_results_loaded}</td>
                    <td>{u.created_at ? new Date(u.created_at).toLocaleString() : '--'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Expected Results Summary */}
      {hasExpected && (
        <section className={styles.section}>
          <h2 className={styles.sectionTitle}>Loaded Expected Results</h2>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Measure</th>
                <th>Test Patients</th>
                <th>Period</th>
              </tr>
            </thead>
            <tbody>
              {expected.measures.map((m) => (
                <tr key={m.measure_url}>
                  <td className={styles.measureUrl}>{m.measure_url}</td>
                  <td>{m.patient_count}</td>
                  <td>{m.period_start} to {m.period_end}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {/* Run Validation */}
      <section className={styles.section}>
        <div className={styles.runHeader}>
          <h2 className={styles.sectionTitle}>Validation Runs</h2>
          <button
            onClick={handleRunValidation}
            disabled={!hasExpected || runStarting}
            className={styles.primaryBtn}
            title={!hasExpected ? 'Upload a test bundle first' : ''}
          >
            {runStarting ? 'Starting...' : 'Run Validation'}
          </button>
        </div>

        {runs.length > 0 && (
          <div className={styles.runSelector}>
            <label className={styles.runLabel}>View run:</label>
            <select
              value={selectedRunId}
              onChange={e => setSelectedRunId(e.target.value)}
              className={styles.runSelect}
            >
              <option value="">Select a run...</option>
              {runs.map(r => (
                <option key={r.id} value={r.id}>
                  Run #{r.id} — {r.status} — {r.created_at ? new Date(r.created_at).toLocaleString() : ''}
                </option>
              ))}
            </select>
          </div>
        )}
      </section>

      {/* Results Dashboard */}
      {runDetail && (
        <section className={styles.section}>
          {runDetail.status === 'running' || runDetail.status === 'queued' ? (
            <div className={styles.runningBanner}>
              Validation {runDetail.status}... ({runDetail.patients_tested || 0} patients)
            </div>
          ) : runDetail.status === 'failed' ? (
            <div className={styles.errorBanner} role="alert">
              Validation failed: {runDetail.error_message || 'Unknown error'}
            </div>
          ) : (
            <>
              {/* Summary Cards */}
              <div className={styles.cardsRow}>
                <SummaryCard label="Total Patients" value={runDetail.patients_tested} />
                <SummaryCard label="Passing" value={runDetail.patients_passed} variant="pass" />
                <SummaryCard label="Failing" value={runDetail.patients_failed} variant="fail" />
                <SummaryCard label="Measures" value={runDetail.measures_tested} />
              </div>

              {/* Per-measure results */}
              {(runDetail.measures || []).map((measure, mi) => (
                <div key={mi} className={styles.measureSection}>
                  <div className={styles.measureHeader}>
                    <span className={styles.measureName}>
                      {measure.measure_url.split('/').pop()}
                    </span>
                    <span className={`${styles.measureBadge} ${measure.failed + measure.errors > 0 ? styles.badgeFail : styles.badgePass}`}>
                      {measure.passed} / {measure.patients.length} PASS
                    </span>
                  </div>

                  <table className={styles.table}>
                    <thead>
                      <tr>
                        <th style={{width: '30px'}}></th>
                        <th>Patient</th>
                        <th className={styles.popCell}>Init Pop</th>
                        <th className={styles.popCell}>Denom</th>
                        <th className={styles.popCell}>Denom Excl</th>
                        <th className={styles.popCell}>Numer</th>
                      </tr>
                    </thead>
                    <tbody>
                      {measure.patients.map((p, pi) => (
                        <tr key={pi} className={p.status === 'fail' ? styles.failRow : p.status === 'error' ? styles.errorRow : ''}>
                          <td>
                            {p.status === 'pass' && <span className={styles.passIcon}>&#10003;</span>}
                            {p.status === 'fail' && <span className={styles.failIcon}>&#10007;</span>}
                            {p.status === 'error' && <span className={styles.errorIcon}>!</span>}
                          </td>
                          <td>
                            <div>{p.patient_name || p.patient_ref}</div>
                            {p.status === 'error' && <div className={styles.errorText}>{p.error_message}</div>}
                          </td>
                          {['initial-population', 'denominator', 'denominator-exclusion', 'numerator'].map(code => {
                            const exp = p.expected_populations?.[code];
                            const act = p.actual_populations?.[code];
                            const mismatch = (p.mismatches || []).includes(code);
                            return (
                              <td key={code} className={styles.popCell}>
                                <span className={mismatch ? styles.mismatch : styles.match}>
                                  {act !== undefined ? act : '--'}
                                </span>
                                <br />
                                <span className={styles.expected}>exp: {exp !== undefined ? exp : '--'}</span>
                              </td>
                            );
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ))}
            </>
          )}
        </section>
      )}
    </div>
  );
}
