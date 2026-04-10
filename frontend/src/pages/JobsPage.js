import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Link } from 'react-router-dom';
import styles from './JobsPage.module.css';
import { getJobs, getMeasures, getGroups, createJob, cancelJob } from '../api/client';
import { useToast } from '../components/Toast';
import ProgressBar from '../components/ProgressBar';

function formatDateTime(dateStr) {
  if (!dateStr) return '--';
  const d = new Date(dateStr);
  return d.toLocaleString(undefined, {
    month: 'short', day: 'numeric', year: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
}

function formatElapsed(startStr) {
  if (!startStr) return '--';
  const start = new Date(startStr);
  const now = new Date();
  const diffSec = Math.floor((now - start) / 1000);
  const min = Math.floor(diffSec / 60);
  const sec = diffSec % 60;
  return `${min}m ${sec.toString().padStart(2, '0')}s`;
}

function estimateRemaining(startStr, progress) {
  if (!startStr || !progress || progress >= 100) return null;
  const start = new Date(startStr);
  const now = new Date();
  const elapsed = now - start;
  const total = (elapsed / progress) * 100;
  const remaining = total - elapsed;
  const min = Math.floor(remaining / 60000);
  return `~${min}m`;
}

function StatusBadge({ status }) {
  const normalized = (status || '').toLowerCase();
  let variant = styles.statusDefault;
  if (normalized === 'completed' || normalized === 'complete') variant = styles.statusSuccess;
  else if (normalized === 'running' || normalized === 'in_progress' || normalized === 'in-progress') variant = styles.statusRunning;
  else if (normalized === 'queued' || normalized === 'pending') variant = styles.statusQueued;
  else if (normalized === 'failed' || normalized === 'error') variant = styles.statusError;
  else if (normalized === 'cancelled' || normalized === 'canceled') variant = styles.statusError;

  return <span className={`${styles.statusBadge} ${variant}`}>{status}</span>;
}

function isRunning(status) {
  const s = (status || '').toLowerCase();
  return s === 'running' || s === 'in_progress' || s === 'in-progress' || s === 'queued' || s === 'pending';
}

export default function JobsPage() {
  const [jobs, setJobs] = useState([]);
  const [measures, setMeasures] = useState([]);
  const [groups, setGroups] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [showModal, setShowModal] = useState(false);
  const [formData, setFormData] = useState({ measure_id: '', group_id: '', period_start: '', period_end: '' });
  const [creating, setCreating] = useState(false);
  const pollRef = useRef(null);
  const toast = useToast();

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
      if (list.length > 0 && !formData.measure_id) {
        setFormData(prev => ({ ...prev, measure_id: list[0].id || '' }));
      }
    } catch {
      // Non-blocking
    }
  }, [formData.measure_id]);

  const loadGroups = useCallback(async () => {
    try {
      const data = await getGroups();
      setGroups(data.groups || []);
    } catch {
      // Non-blocking — groups are optional
    }
  }, []);

  useEffect(() => {
    loadJobs();
    loadMeasures();
    loadGroups();
  }, [loadJobs, loadMeasures, loadGroups]);

  // Polling for in-progress jobs
  useEffect(() => {
    const hasRunning = jobs.some(j => isRunning(j.status));
    if (hasRunning) {
      pollRef.current = setInterval(loadJobs, 3000);
    } else {
      if (pollRef.current) clearInterval(pollRef.current);
    }
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [jobs, loadJobs]);

  const handleCreateJob = async (e) => {
    e.preventDefault();
    if (!formData.measure_id) {
      toast.error('Please select a measure');
      return;
    }
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
      toast.error(`Failed to cancel job: ${err.message}`);
    }
  };

  const getMeasureName = (job) => {
    if (job.measure_name) return job.measure_name;
    const m = measures.find(m => m.id === job.measure_id);
    if (m) return m.resource?.title || m.resource?.name || m.title || m.name || job.measure_id;
    return job.measure_id || '--';
  };

  const getProgress = (job) => {
    if (job.progress !== undefined && job.progress !== null) return job.progress;
    if (job.patients_processed && job.total_patients) {
      return Math.round((job.patients_processed / job.total_patients) * 100);
    }
    return 0;
  };

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <h1 className={styles.title}>Jobs</h1>
        <button className={styles.newBtn} onClick={() => setShowModal(true)}>
          New Calculation
        </button>
      </div>

      {/* Loading */}
      {loading && (
        <div className={styles.tableWrapper} role="status" aria-label="Loading jobs">
          <table>
            <thead>
              <tr>
                <th>Measure</th>
                <th>CDR</th>
                <th>Period</th>
                <th>Status</th>
                <th>Progress</th>
                <th>Started</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {[1, 2, 3].map(i => (
                <tr key={i}>
                  {[1, 2, 3, 4, 5, 6, 7].map(j => (
                    <td key={j}><div className={`skeleton ${styles.skeletonCell}`} /></td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Error */}
      {!loading && error && (
        <div className={styles.errorState} role="alert">
          <p>{error}</p>
          <button className={styles.retryBtn} onClick={loadJobs}>Retry</button>
        </div>
      )}

      {/* Jobs table */}
      {!loading && !error && (
        <div className={styles.tableWrapper}>
          <table aria-label="Calculation jobs">
            <thead>
              <tr>
                <th>Measure</th>
                <th>CDR</th>
                <th>Period</th>
                <th>Status</th>
                <th>Progress</th>
                <th>Started</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {jobs.length === 0 ? (
                <tr>
                  <td colSpan={7} className={styles.emptyRow}>
                    No calculations yet. Select a measure and click Calculate.
                  </td>
                </tr>
              ) : (
                jobs.map((job) => {
                  const running = isRunning(job.status);
                  const progress = getProgress(job);
                  const completed = (job.status || '').toLowerCase() === 'completed' || (job.status || '').toLowerCase() === 'complete';

                  return (
                    <tr key={job.id}>
                      <td className={styles.measureCell}>{getMeasureName(job)}</td>
                      <td title={job.cdr_url || undefined}>
                        {job.cdr_name || job.cdr_url || 'Default'}
                        {job.cdr_read_only && <span className={styles.cdrReadOnly}>(read-only)</span>}
                      </td>
                      <td className={styles.periodCell}>
                        {job.period_start && job.period_end
                          ? `${job.period_start} - ${job.period_end}`
                          : '--'}
                      </td>
                      <td><StatusBadge status={job.status} /></td>
                      <td className={styles.progressCell}>
                        {running ? (
                          <div className={styles.progressInfo}>
                            <ProgressBar value={progress} max={100} size="sm" />
                            <div className={styles.progressMeta}>
                              {job.patients_processed !== undefined && job.total_patients !== undefined && (
                                <span>{job.patients_processed.toLocaleString()} / {job.total_patients.toLocaleString()} patients</span>
                              )}
                              {job.batches_completed !== undefined && job.total_batches !== undefined && (
                                <span>{job.batches_completed}/{job.total_batches} batches</span>
                              )}
                              <span>{formatElapsed(job.started_at || job.created_at)}</span>
                              {estimateRemaining(job.started_at || job.created_at, progress) && (
                                <span>Est. {estimateRemaining(job.started_at || job.created_at, progress)}</span>
                              )}
                            </div>
                          </div>
                        ) : completed ? (
                          <span className={styles.completedText}>100%</span>
                        ) : (
                          '--'
                        )}
                      </td>
                      <td className={styles.dateCell}>{formatDateTime(job.started_at || job.created_at)}</td>
                      <td>
                        {running && (
                          <button
                            className={styles.cancelBtn}
                            onClick={() => handleCancel(job.id)}
                          >
                            Cancel
                          </button>
                        )}
                        {completed && (
                          <Link to={`/results/${job.id}`} className={styles.viewLink}>
                            View Results
                          </Link>
                        )}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* New Calculation Modal */}
      {showModal && (
        <div className={styles.modalOverlay} onClick={() => setShowModal(false)} role="dialog" aria-label="New calculation" aria-modal="true">
          <div className={styles.modal} onClick={e => e.stopPropagation()}>
            <div className={styles.modalHeader}>
              <h2 className={styles.modalTitle}>New Calculation</h2>
              <button className={styles.modalClose} onClick={() => setShowModal(false)} aria-label="Close">
                &times;
              </button>
            </div>
            <form onSubmit={handleCreateJob} className={styles.modalBody}>
              <div className={styles.formGroup}>
                <label htmlFor="measure-select" className={styles.label}>Measure</label>
                <select
                  id="measure-select"
                  value={formData.measure_id}
                  onChange={e => setFormData(prev => ({ ...prev, measure_id: e.target.value }))}
                  className={styles.select}
                  required
                >
                  <option value="" disabled>Select a measure</option>
                  {measures.map((m, i) => (
                    <option key={m.id || i} value={m.id || ''}>
                      {m.resource?.title || m.resource?.name || m.title || m.name || m.id}
                    </option>
                  ))}
                </select>
              </div>
              <div className={styles.formGroup}>
                <label htmlFor="group-select" className={styles.label}>Patient Group <span className={styles.labelHint}>(optional)</span></label>
                <select
                  id="group-select"
                  value={formData.group_id}
                  onChange={e => setFormData(prev => ({ ...prev, group_id: e.target.value }))}
                  className={styles.select}
                >
                  <option value="">All Patients (no group filter)</option>
                  {groups.map(g => (
                    <option key={g.id} value={g.id}>
                      {g.name || g.id} ({g.member_count} patients)
                    </option>
                  ))}
                </select>
              </div>
              <div className={styles.formRow}>
                <div className={styles.formGroup}>
                  <label htmlFor="period-start" className={styles.label}>Period Start</label>
                  <input
                    id="period-start"
                    type="date"
                    value={formData.period_start}
                    onChange={e => setFormData(prev => ({ ...prev, period_start: e.target.value }))}
                    className={styles.input}
                  />
                </div>
                <div className={styles.formGroup}>
                  <label htmlFor="period-end" className={styles.label}>Period End</label>
                  <input
                    id="period-end"
                    type="date"
                    value={formData.period_end}
                    onChange={e => setFormData(prev => ({ ...prev, period_end: e.target.value }))}
                    className={styles.input}
                  />
                </div>
              </div>
              <div className={styles.modalFooter}>
                <button
                  type="button"
                  className={styles.secondaryBtn}
                  onClick={() => setShowModal(false)}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className={styles.primaryBtn}
                  disabled={creating}
                  aria-busy={creating}
                >
                  {creating ? 'Starting...' : 'Calculate'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
