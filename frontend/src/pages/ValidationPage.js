import React, { useState, useEffect, useCallback, useRef } from 'react';
import styles from './ValidationPage.module.css';
import {
  deleteValidationRun, uploadTestBundle, getUploads,
  getExpectedResults, startValidationRun, getValidationRuns, getValidationRun,
} from '../api/client';
import { useToast } from '../components/Toast';
import KebabMenu from '../components/KebabMenu';
import ConfirmDialog from '../components/ConfirmDialog';
import { TrashIcon, SparkIcon, ViewIcon, XIcon, ChevronIcon } from '../components/Icons';
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
  const expectedMeasures = expected?.measures || [];

  const q = query.trim().toLowerCase();
  const filteredRuns = runs.filter(r => {
    if (!q) return true;
    return String(r.id).includes(q) || (r.status || '').toLowerCase().includes(q);
  });

  // KPI values
  const bundlesLoaded = uploads.length;
  const uploadPatients = uploads.reduce((sum, u) => sum + (u.patients_loaded || 0), 0);
  const totalPatients = runs.reduce((sum, r) => sum + (r.patients_tested || 0), 0) || uploadPatients;
  const totalPassedPatients = runs.reduce((sum, r) => sum + (r.patients_passed || 0), 0);
  const passRate = totalPatients > 0 ? `${((totalPassedPatients / totalPatients) * 100).toFixed(1)}%` : '--';

  const measureLabel = (measureUrl) => {
    if (!measureUrl) return '--';
    return measureUrl.split('/').pop() || measureUrl;
  };

  const runBundleName = (run, index) => (
    run.bundle_filename
    || run.filename
    || uploads[index]?.filename
    || uploads[0]?.filename
    || `Run #${run.id}`
  );

  const runMeasureName = (run) => {
    const measureUrl = run.measure_urls?.[0] || expectedMeasures[0]?.measure_url;
    if (measureUrl) return measureLabel(measureUrl);
    if (run.measures_tested) return `${run.measures_tested} measure${run.measures_tested === 1 ? '' : 's'}`;
    return '--';
  };

  const selectedRunSummary = runs.find(r => String(r.id) === String(selectedRunId));
  const selectedRunIndex = selectedRunSummary ? runs.findIndex(r => String(r.id) === String(selectedRunId)) : -1;

  return (
    <div className={styles.page}>
      <div className={styles.pageHeader}>
        <div>
          <div className={styles.eyebrow}>Testing</div>
          <h1 className={styles.title}>Validation</h1>
          <div className={styles.sub}>Run measures against known test bundles and compare to expected population membership.</div>
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

      {/* Upload result banners — show failed/warned uploads */}
      {uploads.filter(u => u.status === 'failed' || u.warning_message).slice(0, 3).map(u => (
        <div key={u.id} className={`${styles.errorBanner} ${u.status !== 'failed' ? styles.warnBanner : ''}`} role="alert">
          <strong>{u.filename}</strong>
          {u.status === 'failed'
            ? ` — Upload failed: ${u.error_message || 'Unknown error'}`
            : ` — ${u.warning_message}`}
          {u.error_details?.failed_entries?.length > 0 && (
            <ul className={styles.uploadErrorList}>
              {u.error_details.failed_entries.map((fe, i) => (
                <li key={i}>
                  <span className={styles.uploadErrorType}>{fe.resource_type}{fe.resource_id ? `/${fe.resource_id}` : ''}</span>
                  {' '}
                  <span className={styles.uploadErrorStatus}>{fe.status}</span>
                  {fe.diagnostics && <span className={styles.uploadErrorDiag}> — {fe.diagnostics}</span>}
                </li>
              ))}
            </ul>
          )}
        </div>
      ))}

      {/* KPI cards */}
      <div className={styles.kpiRow}>
        {[
          ['Bundles loaded', String(bundlesLoaded || expected?.total_measures || 0), 'text'],
          ['Test patients', totalPatients ? String(totalPatients) : '--', 'text'],
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
          <span className={styles.cardTitle}>Recent validation runs</span>
        </div>
        <table aria-label="Validation runs">
          <thead>
            <tr>
              <th>Bundle</th>
              <th>Measure</th>
              <th>Pass</th>
              <th>Fail</th>
              <th>Error</th>
              <th>When</th>
              <th>Status</th>
              <th style={{ width: 40 }}></th>
            </tr>
          </thead>
          <tbody>
            {filteredRuns.length === 0 ? (
              <tr><td colSpan={8} className={styles.emptyRow}>
                {q ? `No runs match "${q}".` : 'No validation runs yet. Upload a test bundle and click "New run".'}
              </td></tr>
            ) : (
              filteredRuns.map((r, i) => {
                const deleting = r.delete_requested || deletingRunIds.includes(String(r.id));
                return (
                  <tr key={r.id} className={styles.row}>
                    <td data-label="Bundle" className={styles.bundleCell}>{runBundleName(r, i)}</td>
                    <td data-label="Measure" className={styles.measureCell}>{runMeasureName(r)}</td>
                    <td data-label="Pass" className={styles.passCount}>{r.patients_passed ?? '--'}</td>
                    <td data-label="Fail" className={styles.failCount}>{r.patients_failed ?? '--'}</td>
                    <td data-label="Error" className={styles.errorCount}>{r.patients_error ?? r.errors ?? 0}</td>
                    <td data-label="When" className={styles.dateCell}>{r.created_at ? new Date(r.created_at).toLocaleString() : '--'}</td>
                    <td data-label="Status"><StatusBadge status={deleting ? 'deleting' : r.status} /></td>
                    <td data-label="Actions">
                      <div className={styles.rowActions}>
                        <button
                          type="button"
                          className={styles.viewDetailsBtn}
                          onClick={() => setSelectedRunId(String(r.id))}
                        >
                          View details
                        </button>
                        <KebabMenu items={[
                          {
                            label: 'View details',
                            icon: <ViewIcon />,
                            onClick: () => setSelectedRunId(String(r.id)),
                          },
                          { divider: true },
                          {
                            label: 'Delete run',
                            icon: <TrashIcon />,
                            tone: 'destructive',
                            disabled: deleting,
                            onClick: () => setConfirmRun(r),
                          },
                        ]} />
                      </div>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {runDetail && (
        <ValidationDetailsDrawer
          run={runDetail}
          bundle={runBundleName(selectedRunSummary || runDetail, selectedRunIndex >= 0 ? selectedRunIndex : 0)}
          measure={runMeasureName(selectedRunSummary || runDetail)}
          onRerun={handleRunValidation}
          rerunning={runStarting}
          onClose={() => setSelectedRunId('')}
        />
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

const POPULATION_LABELS = {
  'initial-population': 'IP',
  denominator: 'DENOM',
  'denominator-exclusion': 'EXCL',
  numerator: 'NUMER',
  'numerator-exclusion': 'NUMEX',
};

function populationList(populations) {
  if (!populations) return [];
  return Object.entries(POPULATION_LABELS)
    .filter(([code]) => Boolean(populations[code]))
    .map(([, label]) => label);
}

function measureNameFromUrl(url) {
  if (!url) return '--';
  return url.split('/').pop() || url;
}

function toCaseId(patient, measureIndex, patientIndex) {
  if (patient.patient_ref) return patient.patient_ref.split('/').pop() || patient.patient_ref;
  return `case-${measureIndex + 1}-${patientIndex + 1}`;
}

function flattenValidationCases(run) {
  return (run.measures || []).flatMap((measure, measureIndex) => (
    (measure.patients || []).map((patient, patientIndex) => {
      const expected = populationList(patient.expected_populations);
      const actual = populationList(patient.actual_populations);
      const mismatches = patient.mismatches || [];
      return {
        id: toCaseId(patient, measureIndex, patientIndex),
        name: patient.patient_name || patient.patient_ref || 'Unknown patient',
        measure: measureNameFromUrl(measure.measure_url),
        status: (patient.status || 'unknown').toLowerCase(),
        expected,
        actual,
        error: patient.error_message,
        mismatches: mismatches.map(code => POPULATION_LABELS[code] || code),
      };
    })
  ));
}

function downloadValidationReport(run, bundle, measure, cases) {
  const report = {
    id: run.id,
    status: run.status,
    bundle,
    measure,
    created_at: run.created_at,
    completed_at: run.completed_at,
    patients_tested: run.patients_tested,
    patients_passed: run.patients_passed,
    patients_failed: run.patients_failed,
    measures_tested: run.measures_tested,
    cases,
  };
  const blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `validation-run-${run.id || 'report'}.json`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function ValidationDetailsDrawer({ run, bundle, measure, onClose, onRerun, rerunning }) {
  const [tab, setTab] = useState('all');
  const cases = flattenValidationCases(run);
  const pass = run.patients_passed ?? (run.measures || []).reduce((sum, m) => sum + (m.passed || 0), 0);
  const errorCount = (run.measures || []).reduce((sum, m) => sum + (m.errors || 0), 0);
  const fail = (run.measures || []).reduce((sum, m) => sum + (m.failed || 0), 0);
  const total = cases.length || run.patients_tested || pass + fail + errorCount || 0;
  const passPct = total > 0 ? Math.round((pass / total) * 100) : 0;
  const when = run.created_at ? new Date(run.created_at).toLocaleString() : `Run #${run.id}`;
  const isActive = run.status === 'queued' || run.status === 'running';
  const filteredCases = cases.filter(c => tab === 'all' || c.status === tab);
  const passRateClass = passPct >= 90 ? styles.drawerStatOk : passPct >= 70 ? styles.drawerStatWarn : styles.drawerStatErr;

  return (
    <div className={styles.drawerOverlay} onClick={onClose} role="presentation">
      <aside className={styles.drawer} aria-label={`Validation run ${run.id} details`} onClick={event => event.stopPropagation()}>
        <div className={styles.drawerHeader}>
          <div className={styles.drawerHeaderTop}>
            <div className={styles.drawerTitleBlock}>
              <div className={styles.drawerEyebrow}>Validation run · {when}</div>
              <h2 className={styles.drawerTitle}>{bundle}</h2>
              <div className={styles.drawerMeta}>{measure} · {total} test cases</div>
            </div>
            <button type="button" className={styles.drawerClose} onClick={onClose} aria-label="Close validation details">
              <XIcon />
            </button>
          </div>
          <div className={styles.drawerSummary}>
            <div>
              <div className={styles.drawerStatLabel}>Pass rate</div>
              <div className={`${styles.drawerPassRate} ${passRateClass}`}>
                {total > 0 ? passPct : '--'}{total > 0 && <span>%</span>}
              </div>
            </div>
            <div className={styles.drawerCounts}>
              <div>
                <div className={styles.drawerStatLabel}>Pass</div>
                <div className={`${styles.drawerCount} ${styles.drawerStatOk}`}>{pass}</div>
              </div>
              <div>
                <div className={styles.drawerStatLabel}>Fail</div>
                <div className={`${styles.drawerCount} ${fail ? styles.drawerStatErr : ''}`}>{fail}</div>
              </div>
              <div>
                <div className={styles.drawerStatLabel}>Error</div>
                <div className={`${styles.drawerCount} ${errorCount ? styles.drawerStatWarn : ''}`}>{errorCount}</div>
              </div>
            </div>
          </div>
        </div>

        <div className={styles.drawerTabs} role="tablist" aria-label="Validation case filters">
          {[
            ['all', `All ${total}`],
            ['fail', `Failures ${fail}`],
            ['error', `Errors ${errorCount}`],
            ['pass', `Passing ${pass}`],
          ].map(([key, label]) => (
            <button
              key={key}
              type="button"
              role="tab"
              aria-selected={tab === key}
              className={`${styles.drawerTab} ${tab === key ? styles.drawerTabActive : ''}`}
              onClick={() => setTab(key)}
            >
              {label}
            </button>
          ))}
        </div>

        <div className={styles.drawerBody}>
          {isActive && (
            <div className={styles.drawerNotice}>
              Validation {run.status} ({run.patients_tested || 0} patients tested)
            </div>
          )}
          {run.status === 'failed' && (
            <div className={styles.drawerError}>
              Validation failed: {run.error_message || 'Unknown error'}
            </div>
          )}
          {!isActive && run.status !== 'failed' && filteredCases.length === 0 && (
            <div className={styles.drawerEmpty}>No {tab === 'all' ? '' : tab} cases in this run.</div>
          )}
          {filteredCases.map(c => (
            <ValidationCaseRow key={`${c.measure}-${c.id}`} c={c} />
          ))}
        </div>

        <div className={styles.drawerFooter}>
          <button type="button" className={styles.drawerButton} onClick={() => downloadValidationReport(run, bundle, measure, cases)}>
            Download report
          </button>
          <button
            type="button"
            className={`${styles.drawerButton} ${styles.drawerButtonPrimary}`}
            onClick={onRerun}
            disabled={rerunning}
          >
            <SparkIcon /> {rerunning ? 'Starting...' : 'Re-run'}
          </button>
        </div>
      </aside>
    </div>
  );
}

function ValidationCaseRow({ c }) {
  const [open, setOpen] = useState(false);
  const hasDetail = c.status === 'fail' || c.status === 'error' || c.mismatches.length > 0;
  const statusClass = c.status === 'pass'
    ? styles.caseDotPass
    : c.status === 'fail'
      ? styles.caseDotFail
      : styles.caseDotError;

  return (
    <div
      className={`${styles.caseRow} ${hasDetail ? styles.caseRowInteractive : ''}`}
      onClick={() => hasDetail && setOpen(value => !value)}
      role={hasDetail ? 'button' : undefined}
      tabIndex={hasDetail ? 0 : undefined}
      onKeyDown={(event) => {
        if (!hasDetail) return;
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          setOpen(value => !value);
        }
      }}
    >
      <div className={styles.caseMain}>
        <span className={`${styles.caseDot} ${statusClass}`} aria-hidden="true" />
        <span className={styles.caseId}>{c.id}</span>
        <span className={styles.caseName}>{c.name}</span>
        {c.status === 'error' ? (
          <span className={styles.caseErrorLabel}>error</span>
        ) : (
          <span className={`${styles.caseResult} ${c.status === 'fail' ? styles.caseResultMismatch : ''}`}>
            {c.expected.join(', ') || 'none'}
            <span> &rarr; </span>
            <strong>{c.actual.join(', ') || 'none'}</strong>
          </span>
        )}
        {hasDetail && (
          <ChevronIcon className={`${styles.caseChevron} ${open ? styles.caseChevronOpen : ''}`} />
        )}
      </div>
      {open && c.status === 'error' && (
        <div className={styles.caseError}>{c.error || 'Unexpected validation error.'}</div>
      )}
      {open && c.status !== 'error' && (
        <div className={styles.caseDetail}>
          <div>Measure: <span>{c.measure}</span></div>
          <div>Expected: <span>{c.expected.join(', ') || 'none'}</span></div>
          <div>Calculated: <span>{c.actual.join(', ') || 'none'}</span></div>
          {c.mismatches.length > 0 && <div>Mismatch: <span>{c.mismatches.join(', ')}</span></div>}
        </div>
      )}
    </div>
  );
}
