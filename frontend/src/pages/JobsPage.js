import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Link, useNavigate, useLocation } from 'react-router-dom';
import styles from './JobsPage.module.css';
import { getJobs, getMeasures, getGroups, createJob, cancelJob, deleteJob } from '../api/client';
import { useToast } from '../components/Toast';
import KebabMenu from '../components/KebabMenu';
import ConfirmDialog from '../components/ConfirmDialog';
import PulseDot from '../components/PulseDot';
import { TrashIcon, ViewIcon, SparkIcon, PlusIcon, XIcon } from '../components/Icons';
import { useSearch } from '../contexts/SearchContext';
import PeriodPicker from '../components/PeriodPicker';
import { extractCmsId, measureDisplayLabel, measureOptionLabel, findMatchingGroup } from '../utils/measureFormat';

function formatDateTime(dateStr) {
  if (!dateStr) return '--';
  const d = new Date(dateStr);
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function formatElapsed(startStr) {
  if (!startStr) return '--';
  const diff = Math.floor((Date.now() - new Date(startStr)) / 1000);
  const m = Math.floor(diff / 60);
  const s = diff % 60;
  return `${m}m ${String(s).padStart(2, '0')}s`;
}

function estimateRemaining(startStr, progress) {
  if (!startStr || !progress || progress >= 100) return null;
  const elapsed = Date.now() - new Date(startStr);
  const total = (elapsed / progress) * 100;
  const rem = Math.floor((total - elapsed) / 60000);
  return rem > 0 ? `~${rem}m remaining` : null;
}

function StatusBadge({ status }) {
  const s = (status || '').toLowerCase();
  if (s === 'completed' || s === 'complete') return <span className={`${styles.badge} ${styles.badgeOk}`}>Complete</span>;
  if (s === 'running' || s === 'in_progress' || s === 'in-progress') return <span className={`${styles.badge} ${styles.badgeRunning}`}><PulseDot />Running</span>;
  if (s === 'queued' || s === 'pending') return <span className={`${styles.badge} ${styles.badgeInfo}`}>Queued</span>;
  if (s === 'failed' || s === 'error') return <span className={`${styles.badge} ${styles.badgeErr}`}>Failed</span>;
  if (s === 'cancelled' || s === 'canceled') return <span className={`${styles.badge} ${styles.badgeErr}`}>Cancelled</span>;
  return <span className={styles.badge}>{status}</span>;
}

function isRunning(status) {
  const s = (status || '').toLowerCase();
  return s === 'running' || s === 'in_progress' || s === 'in-progress' || s === 'queued' || s === 'pending';
}

function isComplete(status) {
  const s = (status || '').toLowerCase();
  return s === 'completed' || s === 'complete';
}

export default function JobsPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const [jobs, setJobs] = useState([]);
  const [measures, setMeasures] = useState([]);
  const [groups, setGroups] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [showModal, setShowModal] = useState(false);
  const [formData, setFormData] = useState({ measure_id: '', group_id: '', period_start: '', period_end: '' });
  const [creating, setCreating] = useState(false);
  const [confirmJob, setConfirmJob] = useState(null);
  const [deletingJobIds, setDeletingJobIds] = useState([]);
  const pollRef = useRef(null);
  const toast = useToast();
  const { query } = useSearch();

  const loadJobs = useCallback(async () => {
    try {
      const data = await getJobs();
      setJobs(Array.isArray(data) ? data : data.jobs || []);
      setError(null);
    } catch (err) {
      setError(err.message || 'Failed to load jobs');
    } finally {
      setLoading(false);
    }
  }, []);

  const loadMeasures = useCallback(async () => {
    try {
      const data = await getMeasures();
      const list = Array.isArray(data) ? data : data.measures || data.entry || [];
      setMeasures(list);
    } catch { /* non-blocking */ }
  }, []);

  const loadGroups = useCallback(async () => {
    try {
      const data = await getGroups();
      setGroups(data.groups || []);
    } catch { /* non-blocking */ }
  }, []);

  useEffect(() => {
    loadJobs();
    loadMeasures();
    loadGroups();
  }, [loadJobs, loadMeasures, loadGroups]);

  useEffect(() => {
    if (measures.length > 0 && !formData.measure_id) {
      setFormData(prev => ({ ...prev, measure_id: measures[0].id || '' }));
    }
  }, [measures]);

  useEffect(() => {
    const hasActive = jobs.some(j => isRunning(j.status) || j.delete_requested);
    if (hasActive) {
      pollRef.current = setInterval(loadJobs, 3000);
    } else {
      if (pollRef.current) clearInterval(pollRef.current);
    }
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [jobs, loadJobs]);

  useEffect(() => {
    if (!showModal) return;
    const y = new Date().getFullYear();
    setFormData(p => ({ ...p, period_start: `${y}-01-01`, period_end: `${y}-12-31` }));
  }, [showModal]);

  useEffect(() => {
    if (!formData.measure_id || !groups.length) return;
    const match = findMatchingGroup(formData.measure_id, groups);
    setFormData(p => ({ ...p, group_id: match != null ? String(match.id) : '' }));
  }, [formData.measure_id, groups]);

  const handleCreateJob = async (e) => {
    e.preventDefault();
    if (!formData.measure_id) { toast.error('Please select a measure'); return; }
    setCreating(true);
    try {
      await createJob({
        measure_id: formData.measure_id,
        group_id: formData.group_id || undefined,
        period_start: formData.period_start || undefined,
        period_end: formData.period_end || undefined,
      });
      toast.success('Calculation started');
      setShowModal(false);
      setFormData(prev => ({ ...prev, period_start: '', period_end: '' }));
      loadJobs();
    } catch (err) {
      toast.error(`Failed to create job: ${err.message}`);
    } finally {
      setCreating(false);
    }
  };

  const handleCancel = async (jobId) => {
    try {
      await cancelJob(jobId);
      toast.success('Job cancelled');
      loadJobs();
    } catch (err) {
      toast.error(`Failed to cancel: ${err.message}`);
    }
  };

  const handleDeleteConfirmed = async () => {
    const job = confirmJob;
    setConfirmJob(null);
    setDeletingJobIds(prev => [...prev, job.id]);
    try {
      const result = await deleteJob(job.id);
      if (result?.delete_requested) {
        toast.warning('Deletion requested — job will disappear once background work stops.');
      } else {
        toast.success('Job deleted');
      }
      await loadJobs();
    } catch (err) {
      toast.error(`Failed to delete: ${err.message}`);
    } finally {
      setDeletingJobIds(prev => prev.filter(id => id !== job.id));
    }
  };

  const getMeasureName = (job) => {
    const name = job.measure_name || (() => {
      const m = measures.find(m => m.id === job.measure_id);
      return m ? (m.resource?.title || m.resource?.name || m.title || m.name || null) : null;
    })();
    return measureDisplayLabel(job.measure_id, name);
  };

  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const newCalcId = params.get('newCalc');
    if (!newCalcId || !measures.length) return;
    const exists = measures.some(m => m.id === newCalcId);
    setFormData(prev => ({ ...prev, measure_id: exists ? newCalcId : (prev.measure_id || measures[0]?.id || '') }));
    setShowModal(true);
    navigate(location.pathname, { replace: true });
  }, [location.search, measures, navigate, location.pathname]);

  const getProgress = (job) => {
    if (job.progress !== undefined && job.progress !== null) return job.progress;
    const proc = job.processed_patients ?? job.patients_processed ?? 0;
    if (proc && job.total_patients) return Math.round((proc / job.total_patients) * 100);
    return 0;
  };

  const getCohortName = (job) => {
    if (job.group_name) return job.group_name;
    const group = groups.find(g => String(g.id) === String(job.group_id));
    return group?.name || job.cohort || job.group_id || 'All patients';
  };

  const getPatientCount = (job) => {
    const processed = job.processed_patients ?? job.patients_processed;
    const total = job.total_patients;
    if (isRunning(job.status) && total > 0) {
      return `${(processed ?? 0).toLocaleString()} / ${total.toLocaleString()}`;
    }
    if (isRunning(job.status)) return '--';
    if (total > 0) return total.toLocaleString();
    if (processed > 0) return processed.toLocaleString();
    return '--';
  };

  const q = query.trim().toLowerCase();
  const activeJob = jobs.find(j => isRunning(j.status));
  const filteredJobs = jobs.filter(j => {
    if (!q) return true;
    return getMeasureName(j).toLowerCase().includes(q) || (j.id || '').toLowerCase().includes(q);
  });

  return (
    <div className={styles.page}>
      <div className={styles.pageHeader}>
        <div>
          <div className={styles.eyebrow}>Calculations</div>
          <h1 className={styles.title}>Jobs</h1>
          <div className={styles.sub}>
            {jobs.filter(j => isRunning(j.status)).length} running ·{' '}
            {jobs.filter(j => isComplete(j.status)).length} complete ·{' '}
            {jobs.filter(j => (j.status || '').toLowerCase() === 'failed').length} failed
          </div>
        </div>
        <button className={styles.btnPrimary} onClick={() => setShowModal(true)}>
          <PlusIcon /> New calculation
        </button>
      </div>

      {/* Active job hero card */}
      {activeJob && (() => {
        const progress = getProgress(activeJob);
        const proc = activeJob.processed_patients ?? activeJob.patients_processed ?? 0;
        const total = activeJob.total_patients;
        const batches = activeJob.batches_completed ?? 0;
        const totalBatches = activeJob.total_batches ?? 0;
        return (
          <div className={styles.heroCard}>
            <div className={styles.heroTop}>
              <div>
                <div className={styles.heroMeta}>
                  <span className={`${styles.badge} ${styles.badgeRunning}`}><PulseDot />Running</span>
                  <span className={styles.heroId}>{activeJob.id}</span>
                </div>
                <div className={styles.heroName}>{getMeasureName(activeJob)}</div>
                <div className={styles.heroSub}>
                  {activeJob.period_start && activeJob.period_end && (
                    <span className={styles.mono}>{activeJob.period_start} → {activeJob.period_end}</span>
                  )}
                  <span>Cohort: {getCohortName(activeJob)}</span>
                  <span>Elapsed {formatElapsed(activeJob.started_at || activeJob.created_at)}</span>
                </div>
              </div>
              <button className={styles.btnGhost} onClick={() => handleCancel(activeJob.id)}>
                <XIcon /> Cancel
              </button>
            </div>
            <div className={styles.heroProgress}>
              <div className={styles.heroProgressTop}>
                <span className={styles.heroPct}>{progress}<span className={styles.heroPctUnit}>%</span></span>
                {total > 0
                  ? <span className={styles.heroProgressLabel}>{proc.toLocaleString()} of {total.toLocaleString()} patients</span>
                  : <span className={styles.heroProgressLabel}>Preparing…</span>
                }
                {estimateRemaining(activeJob.started_at || activeJob.created_at, progress) && (
                  <span className={styles.heroEta}>{estimateRemaining(activeJob.started_at || activeJob.created_at, progress)}</span>
                )}
              </div>
              <div className={styles.progressTrack}>
                <div className={styles.progressFill} style={{ width: `${progress}%` }} />
              </div>
            </div>
            {totalBatches > 0 && (
              <div className={styles.batchSection}>
                <div className={styles.batchHeader}>
                  <span className={styles.batchLabel}>Batches</span>
                  <span className={styles.batchMeta}>{batches} / {totalBatches}</span>
                </div>
                <div className={styles.batchGrid} style={{ gridTemplateColumns: `repeat(${Math.min(totalBatches, 40)}, 1fr)` }}>
                  {Array.from({ length: Math.min(totalBatches, 40) }, (_, i) => {
                    const s = i < batches ? 'done' : i === batches ? 'active' : 'pending';
                    return <div key={i} className={`${styles.batchCell} ${styles[`batchCell_${s}`]}`} />;
                  })}
                </div>
              </div>
            )}
          </div>
        );
      })()}

      {/* Jobs table */}
      {loading && (
        <div className={styles.card} role="status" aria-label="Loading jobs">
          <table><thead><tr><th>Measure</th><th>Period</th><th>Status</th><th>Started</th><th style={{ width: 50 }}></th></tr></thead>
            <tbody>{[1,2,3].map(i => (<tr key={i}>{[180,100,80,80,40].map((w,j) => (<td key={j}><div className="skeleton" style={{ height: 14, width: w }} /></td>))}</tr>))}</tbody>
          </table>
        </div>
      )}

      {!loading && error && (
        <div className={styles.errorState} role="alert">
          <p>{error}</p>
          <button className={styles.retryBtn} onClick={loadJobs}>Retry</button>
        </div>
      )}

      {!loading && !error && (
        <div className={styles.card}>
          <div className={styles.cardHeader}>
            <span className={styles.cardTitle}>All jobs</span>
            <span className={styles.cardCount}>{filteredJobs.length}</span>
          </div>
          <table aria-label="Calculation jobs">
            <thead>
              <tr>
                <th>Measure</th>
                <th>Period</th>
                <th>Cohort</th>
                <th>Patients</th>
                <th>Status</th>
                <th>Started</th>
                <th style={{ width: 40 }}></th>
              </tr>
            </thead>
            <tbody>
              {filteredJobs.length === 0 ? (
                <tr><td colSpan={7} className={styles.emptyRow}>
                  {q ? `No jobs match "${q}".` : 'No calculations yet. Click "New calculation" to get started.'}
                </td></tr>
              ) : (
                filteredJobs.map((job) => {
                  const running = isRunning(job.status);
                  const complete = isComplete(job.status);
                  const deleting = job.delete_requested || deletingJobIds.includes(job.id);
                  return (
                    <tr
                      key={job.id}
                      className={`${styles.row} ${complete ? styles.rowClickable : ''}`}
                      onClick={() => complete && navigate(`/results/${job.id}`)}
                      style={{ cursor: complete ? 'pointer' : 'default' }}
                    >
                      <td data-label="Measure">
                        <div className={styles.jobMeta}>
                          <div className={styles.jobName}>{getMeasureName(job)}</div>
                          <div className={`${styles.mono} ${styles.jobId}`}>{job.id}</div>
                        </div>
                      </td>
                      <td data-label="Period" className={`${styles.mono} ${styles.periodCell}`}>
                        {job.period_start && job.period_end ? `${job.period_start} – ${job.period_end}` : '--'}
                      </td>
                      <td data-label="Cohort" className={styles.cohortCell}>{getCohortName(job)}</td>
                      <td data-label="Patients" className={styles.patientCountCell}>{getPatientCount(job)}</td>
                      <td data-label="Status"><StatusBadge status={job.status} /></td>
                      <td data-label="Started" className={styles.dateCell}>{formatDateTime(job.started_at || job.created_at)}</td>
                      <td data-label="Actions">
                        <KebabMenu items={[
                          { label: 'View results', icon: <ViewIcon />, disabled: !complete, onClick: () => navigate(`/results/${job.id}`) },
                          { label: 'Re-run', icon: <SparkIcon />, onClick: () => {} },
                          { divider: true },
                          {
                            label: 'Delete',
                            icon: <TrashIcon />,
                            tone: 'destructive',
                            disabled: running || deleting,
                            hint: running ? 'cancel first' : undefined,
                            onClick: () => setConfirmJob(job),
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
      )}

      <ConfirmDialog
        open={!!confirmJob}
        title="Delete this job?"
        body={<><strong>{confirmJob ? getMeasureName(confirmJob) : ''}</strong> and all patient-level results will be deleted. This cannot be undone.</>}
        confirmLabel="Delete job"
        tone="destructive"
        onCancel={() => setConfirmJob(null)}
        onConfirm={handleDeleteConfirmed}
      />

      {/* New Calculation modal */}
      {showModal && (
        <div className={styles.modalBackdrop} onClick={() => setShowModal(false)} role="dialog" aria-label="New calculation" aria-modal="true">
          <div className={styles.sheet} onClick={(e) => e.stopPropagation()}>
            <div className={styles.sheetHeader}>
              <span className={styles.sheetTitle}>New calculation</span>
              <button className={styles.sheetClose} onClick={() => setShowModal(false)} aria-label="Close"><XIcon /></button>
            </div>
            <form id="new-calc-form" onSubmit={handleCreateJob} className={styles.sheetBody}>
              <div className={styles.field}>
                <label className={styles.label} htmlFor="measure-select">Measure</label>
                <select id="measure-select" className={styles.select} value={formData.measure_id}
                  onChange={e => setFormData(p => ({ ...p, measure_id: e.target.value }))} required>
                  <option value="" disabled>Choose a measure…</option>
                  {measures.map((m, i) => (
                    <option key={m.id || i} value={m.id || ''}>{measureOptionLabel(m.id, m.resource?.title || m.resource?.name || m.title || m.name)}</option>
                  ))}
                </select>
              </div>
              <div className={styles.field}>
                <label className={styles.label} htmlFor="group-select">Patient group <span className={styles.labelHint}>(optional)</span></label>
                <select id="group-select" className={styles.select} value={formData.group_id}
                  onChange={e => setFormData(p => ({ ...p, group_id: e.target.value }))}>
                  <option value="">All patients (no group filter)</option>
                  {groups.map(g => {
                    const cmsId = extractCmsId(g.name || g.id);
                    const m = cmsId && measures.find(mx => mx.id === (g.name || g.id));
                    const label = m
                      ? measureOptionLabel(m.id, m.resource?.title || m.resource?.name || m.title || m.name)
                      : (cmsId || g.name || g.id);
                    return <option key={g.id} value={g.id}>{label} ({g.member_count} patients)</option>;
                  })}
                </select>
              </div>
              <PeriodPicker
                periodStart={formData.period_start}
                periodEnd={formData.period_end}
                onChange={(start, end) => setFormData(p => ({ ...p, period_start: start, period_end: end }))}
              />
            </form>
            <div className={styles.sheetFooter}>
              <button type="button" className={styles.btnGhost} onClick={() => setShowModal(false)}>Cancel</button>
              <button type="submit" form="new-calc-form" className={styles.btnPrimary} onClick={handleCreateJob} disabled={creating} aria-busy={creating}>
                {creating ? 'Starting…' : 'Start calculation'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
