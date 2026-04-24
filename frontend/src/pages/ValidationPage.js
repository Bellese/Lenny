import React, { useState, useEffect, useCallback, useRef } from 'react';
import styles from './ValidationPage.module.css';
import {
  deleteValidationRun, uploadTestBundle, getUploads,
  getExpectedResults, startValidationRun, getValidationRuns, getValidationRun,
} from '../api/client';
import { useToast } from '../components/Toast';
import KebabMenu from '../components/KebabMenu';
import ConfirmDialog from '../components/ConfirmDialog';
import { TrashIcon, SparkIcon, PlusIcon } from '../components/Icons';
import { useSearch } from '../contexts/SearchContext';

function StatusBadge({ status }) {
  const s = (status || '').toLowerCase();
  if (s === 'complete' || s === 'completed') return <span className={`${styles.badge} ${styles.badgeOk}`}>Complete</span>;
  if (s === 'running') return <span className={`${styles.badge} ${styles.badgeRunning}`}>Running</span>;
  if (s === 'queued') return <span className={`${styles.badge} ${styles.badgeInfo}`}>Queued</span>;
  if (s === 'failed') return <span className={`${styles.badge} ${styles.badgeErr}`}>Failed</span>;
  return <span className={styles.badge}>{status}</span>;
}

export default function ValidationPage() {
  const [uploads, setUploads] = useState([]);
  const [expected, setExpected] = useState(null);
  const [runs, setRuns] = useState([]);
  const [selectedRunId, setSelectedRunId] = useState('');
  const [runDetail, setRunDetail] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [runStarting, setRunStarting] = useState(false);
  const [confirmRun, setConfirmRun] = useState(null);
  const [deletingRunIds, setDeletingRunIds] = useState([]);
  const [error, setError] = useState(null);
  const fileInputRef = useRef(null);
  const pollRef = useRef(null);
  const toast = useToast();
  const { query } = useSearch();

  const loadData = useCallback(async () => {
    try {
      const [uploadsData, expectedData, runsData] = await Promise.all([
        getUploads(), getExpectedResults(), getValidationRuns(),
      ]);
      setUploads(uploadsData.uploads || []);
      setExpected(expectedData);
      setRuns(runsData.runs || []);
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  useEffect(() => {
    const hasActive = uploads.some(u => u.status === 'queued' || u.status === 'running')
      || runs.some(r => r.status === 'queued' || r.status === 'running' || r.delete_requested);
    if (hasActive) {
      pollRef.current = setInterval(loadData, 3000);
    } else {
      clearInterval(pollRef.current);
    }
    return () => clearInterval(pollRef.current);
  }, [uploads, runs, loadData]);

  useEffect(() => {
    if (!selectedRunId) { setRunDetail(null); return; }
    async function loadDetail() {
      try {
        const detail = await getValidationRun(selectedRunId);
        setRunDetail(detail);
      } catch (err) {
        if (err.status === 404) { setSelectedRunId(''); setRunDetail(null); return; }
        setError(err.message);
      }
    }
    loadDetail();
  }, [selectedRunId]);

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

  const handleDeleteConfirmed = async () => {
    const run = confirmRun;
    setConfirmRun(null);
    const runId = String(run.id);
    setDeletingRunIds(prev => [...prev, runId]);
    if (selectedRunId === runId) { setSelectedRunId(''); setRunDetail(null); }
    try {
      const result = await deleteValidationRun(runId);
      if (result?.delete_requested) {
        toast.warning('Deletion requested — run will disappear once background work stops.');
      } else {
        toast.success('Validation run deleted');
      }
      await loadData();
    } catch (err) {
      setError(err.message);
    } finally {
      setDeletingRunIds(prev => prev.filter(id => id !== runId));
    }
  };

  const hasExpected = expected && expected.total_measures > 0;

  const q = query.trim().toLowerCase();
  const filteredRuns = runs.filter(r => {
    if (!q) return true;
    return String(r.id).includes(q) || (r.status || '').toLowerCase().includes(q);
  });

  // KPI values
  const totalRuns = runs.length;
  const passedRuns = runs.filter(r => r.status === 'complete').length;
  const passRate = totalRuns > 0 ? `${Math.round((passedRuns / totalRuns) * 100)}%` : '--';
  const lastRun = runs[0];

  return (
    <div className={styles.page}>
      <div className={styles.pageHeader}>
        <div>
          <div className={styles.eyebrow}>Testing</div>
          <h1 className={styles.title}>Validation</h1>
          <div className={styles.sub}>Run measures against known test bundles and compare to expected results.</div>
        </div>
        <div className={styles.headerActions}>
          <label className={styles.btnGhost}>
            <input ref={fileInputRef} type="file" accept=".json" className="sr-only" onChange={handleUpload} />
            {uploading ? 'Uploading…' : 'Upload bundle'}
          </label>
          <button className={styles.btnPrimary} onClick={handleRunValidation} disabled={!hasExpected || runStarting}
            title={!hasExpected ? 'Upload a test bundle first' : undefined}>
            <SparkIcon /> {runStarting ? 'Starting…' : 'New run'}
          </button>
        </div>
      </div>

      {error && (
        <div className={styles.errorBanner} role="alert">
          {error}
          <button onClick={() => setError(null)} className={styles.dismissBtn}>Dismiss</button>
        </div>
      )}

      {/* KPI cards */}
      <div className={styles.kpiRow}>
        {[
          ['Total runs', String(totalRuns), 'text'],
          ['Completed', String(passedRuns), 'text'],
          ['Pass rate', passRate, 'accent'],
        ].map(([label, val, tone]) => (
          <div key={label} className={styles.kpiCard}>
            <div className={styles.kpiLabel}>{label}</div>
            <div className={`${styles.kpiVal} ${tone === 'accent' ? styles.kpiValAccent : ''}`}>{val}</div>
          </div>
        ))}
      </div>

      {/* Runs table */}
      <div className={styles.card}>
        <div className={styles.cardHeader}>
          <span className={styles.cardTitle}>Validation runs</span>
        </div>
        <table aria-label="Validation runs">
          <thead>
            <tr>
              <th>Run</th>
              <th>Status</th>
              <th>Patients tested</th>
              <th>Started</th>
              <th style={{ width: 40 }}></th>
            </tr>
          </thead>
          <tbody>
            {filteredRuns.length === 0 ? (
              <tr><td colSpan={5} className={styles.emptyRow}>
                {q ? `No runs match "${q}".` : 'No validation runs yet. Upload a test bundle and click "New run".'}
              </td></tr>
            ) : (
              filteredRuns.map(r => {
                const deleting = r.delete_requested || deletingRunIds.includes(String(r.id));
                return (
                  <tr key={r.id} className={`${styles.row} ${styles.rowClickable}`}
                    onClick={() => setSelectedRunId(String(r.id))}>
                    <td><span className={styles.mono}>#{r.id}</span></td>
                    <td><StatusBadge status={deleting ? 'deleting' : r.status} /></td>
                    <td>{r.patients_tested != null ? r.patients_tested : '--'}</td>
                    <td className={styles.dateCell}>{r.created_at ? new Date(r.created_at).toLocaleString() : '--'}</td>
                    <td>
                      <KebabMenu items={[
                        { divider: true },
                        {
                          label: 'Delete run',
                          icon: <TrashIcon />,
                          tone: 'destructive',
                          disabled: deleting,
                          onClick: () => setConfirmRun(r),
                        },
                      ]} />
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {/* Run detail */}
      {runDetail && (
        <div className={styles.card} style={{ marginTop: 16 }}>
          <div className={styles.cardHeader}>
            <span className={styles.cardTitle}>Run #{runDetail.id}</span>
            <StatusBadge status={runDetail.status} />
          </div>
          {(runDetail.status === 'running' || runDetail.status === 'queued') ? (
            <div className={styles.runningBanner}>
              Validation {runDetail.status}… ({runDetail.patients_tested || 0} patients tested)
            </div>
          ) : runDetail.status === 'failed' ? (
            <div className={styles.errorBanner}>Validation failed: {runDetail.error_message || 'Unknown error'}</div>
          ) : (
            <>
              <div className={styles.detailKpiRow}>
                {[
                  ['Total patients', runDetail.patients_tested],
                  ['Passing', runDetail.patients_passed],
                  ['Failing', runDetail.patients_failed],
                  ['Measures', runDetail.measures_tested],
                ].map(([label, val]) => (
                  <div key={label} className={styles.detailKpi}>
                    <div className={styles.detailKpiLabel}>{label}</div>
                    <div className={styles.detailKpiVal}>{val ?? '--'}</div>
                  </div>
                ))}
              </div>
              {(runDetail.measures || []).map((measure, mi) => (
                <div key={mi} className={styles.measureSection}>
                  <div className={styles.measureSectionHeader}>
                    <span className={styles.measureUrl}>{measure.measure_url.split('/').pop()}</span>
                    <span className={`${styles.badge} ${measure.failed + measure.errors > 0 ? styles.badgeErr : styles.badgeOk}`}>
                      {measure.passed}/{measure.patients.length} PASS
                    </span>
                  </div>
                  <table>
                    <thead>
                      <tr>
                        <th style={{ width: 30 }}></th>
                        <th>Patient</th>
                        <th className={styles.popCell}>Init Pop</th>
                        <th className={styles.popCell}>Denom</th>
                        <th className={styles.popCell}>Excl.</th>
                        <th className={styles.popCell}>Numer</th>
                      </tr>
                    </thead>
                    <tbody>
                      {measure.patients.map((p, pi) => (
                        <tr key={pi} className={p.status === 'fail' ? styles.failRow : p.status === 'error' ? styles.errorRow : ''}>
                          <td>
                            {p.status === 'pass' && <span className={styles.passIcon}>✓</span>}
                            {p.status === 'fail' && <span className={styles.failIcon}>✗</span>}
                            {p.status === 'error' && <span className={styles.warnIcon}>!</span>}
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
                                  {act !== undefined ? String(act) : '--'}
                                </span>
                                <br />
                                <span className={styles.expected}>exp: {exp !== undefined ? String(exp) : '--'}</span>
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
        </div>
      )}

      <ConfirmDialog
        open={!!confirmRun}
        title="Delete this validation run?"
        body={<>Run <strong>#{confirmRun?.id}</strong> and its pass/fail breakdown will be permanently removed.</>}
        confirmLabel="Delete run"
        tone="destructive"
        onCancel={() => setConfirmRun(null)}
        onConfirm={handleDeleteConfirmed}
      />
    </div>
  );
}
