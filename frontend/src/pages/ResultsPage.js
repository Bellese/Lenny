import React, { useState, useEffect, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import styles from './ResultsPage.module.css';
import { getJobs, getResults, getResult, createJob } from '../api/client';
import { useToast } from '../components/Toast';
import PatientDetail from '../components/PatientDetail';
import ComparisonView from '../components/ComparisonView';

function PopulationCard({ label, count, variant = 'default' }) {
  return (
    <div className={`${styles.card} ${styles[variant]}`}>
      <span className={styles.cardLabel}>{label}</span>
      <span className={styles.cardCount}>{count !== undefined && count !== null ? count.toLocaleString() : '--'}</span>
    </div>
  );
}

function CheckMark() {
  return <span className={styles.check} aria-label="Yes" title="Yes">&#10003;</span>;
}

function CrossMark() {
  return <span className={styles.cross} aria-label="No" title="No">&#10007;</span>;
}

function timeAgo(dateStr) {
  if (!dateStr) return null;
  const diff = Math.floor((Date.now() - new Date(dateStr)) / 1000);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function exportCsv(patients, measureName, period) {
  const header = ['Patient ID', 'Name', 'Initial Population', 'Denominator', 'Numerator', 'Denom Exclusion'];
  const rows = patients.map(p => [
    p.patient_id || p.id || '',
    p.patient_name || p.name || '',
    p.populations?.initial_population ? 'Yes' : 'No',
    p.populations?.denominator ? 'Yes' : 'No',
    p.populations?.numerator ? 'Yes' : 'No',
    p.populations?.denominator_exclusion ? 'Yes' : 'No',
  ]);
  const csv = [header, ...rows].map(r => r.map(v => `"${String(v).replace(/"/g, '""')}"`).join(',')).join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `results-${measureName || 'measure'}-${period || 'period'}.csv`.replace(/\s+/g, '-');
  a.click();
  URL.revokeObjectURL(url);
}

const FILTER_OPTIONS = [
  { value: 'all', label: 'All Patients' },
  { value: 'numerator', label: 'In Numerator' },
  { value: 'denominator_only', label: 'Denominator (not Numerator)' },
  { value: 'excluded', label: 'Denominator Exclusion' },
  { value: 'not_in_denominator', label: 'Not in Denominator' },
];

function applyFilter(patients, filter) {
  if (filter === 'all') return patients;
  return patients.filter(p => {
    const pop = p.populations || {};
    if (filter === 'numerator') return pop.numerator;
    if (filter === 'denominator_only') return pop.denominator && !pop.numerator;
    if (filter === 'excluded') return pop.denominator_exclusion;
    if (filter === 'not_in_denominator') return !pop.denominator;
    return true;
  });
}

export default function ResultsPage() {
  const { jobId: routeJobId } = useParams();
  const navigate = useNavigate();
  const toast = useToast();

  const [jobs, setJobs] = useState([]);
  const [selectedJobId, setSelectedJobId] = useState(routeJobId || '');
  const [results, setResults] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selectedPatient, setSelectedPatient] = useState(null);
  const [patientDetail, setPatientDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [populationFilter, setPopulationFilter] = useState('all');
  const [rerunning, setRerunning] = useState(false);

  // Load completed jobs for dropdown
  useEffect(() => {
    async function loadJobs() {
      try {
        const data = await getJobs();
        const all = Array.isArray(data) ? data : data.jobs || [];
        const completed = all.filter(j => {
          const s = (j.status || '').toLowerCase();
          return s === 'completed' || s === 'complete';
        });
        setJobs(completed);
        const preferredId = routeJobId || selectedJobId;
        const preferredExists = preferredId && completed.some(j => String(j.id) === String(preferredId));

        if (preferredExists) {
          if (String(selectedJobId) !== String(preferredId)) {
            setSelectedJobId(String(preferredId));
          }
          return;
        }

        const fallbackId = completed[0] ? String(completed[0].id) : '';
        if (String(selectedJobId) !== fallbackId) {
          setSelectedJobId(fallbackId);
        }
        if (routeJobId && String(routeJobId) !== fallbackId) {
          navigate(fallbackId ? `/results/${fallbackId}` : '/results', { replace: true });
        }
      } catch {
        // Non-blocking
      }
    }
    loadJobs();
  }, [navigate, routeJobId, selectedJobId]);

  // Load results when job is selected
  const loadResults = useCallback(async () => {
    if (!selectedJobId) {
      setResults(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const data = await getResults(selectedJobId);
      setResults(data);
    } catch (err) {
      setError(err.message || 'Error loading results');
    } finally {
      setLoading(false);
    }
  }, [selectedJobId]);

  useEffect(() => {
    loadResults();
  }, [loadResults]);

  const handleJobChange = (e) => {
    const id = e.target.value;
    setSelectedJobId(id);
    setPopulationFilter('all');
    navigate(`/results/${id}`, { replace: true });
  };

  const handleViewPatient = async (patient) => {
    setSelectedPatient(patient);
    if (patient.result_id || patient.id) {
      setDetailLoading(true);
      try {
        const detail = await getResult(patient.result_id || patient.id);
        setPatientDetail(detail);
      } catch {
        setPatientDetail(patient);
      } finally {
        setDetailLoading(false);
      }
    } else {
      setPatientDetail(patient);
    }
  };

  const closeDetail = useCallback(() => {
    setSelectedPatient(null);
    setPatientDetail(null);
  }, []);

  const handleRerun = async () => {
    if (!selectedJob) return;
    setRerunning(true);
    try {
      await createJob({
        measure_id: selectedJob.measure_id,
        measure_name: selectedJob.measure_name,
        group_id: selectedJob.group_id || undefined,
        period_start: selectedJob.period_start || undefined,
        period_end: selectedJob.period_end || undefined,
      });
      toast.success('Re-run started — check the Jobs page');
      navigate('/jobs');
    } catch (err) {
      toast.error(`Failed to start re-run: ${err.message}`);
    } finally {
      setRerunning(false);
    }
  };

  const selectedJob = jobs.find(j => String(j.id) === String(selectedJobId));

  // Extract aggregate data — API returns { populations: {...}, patients: [...], ... }
  const populations = results?.populations || {};
  const allPatients = results?.patients || [];
  const patients = applyFilter(allPatients, populationFilter);
  const measureName = results?.measure_name || selectedJob?.measure_name || selectedJob?.measure_id || '';
  const period = selectedJob?.period_start && selectedJob?.period_end
    ? `${selectedJob.period_start} to ${selectedJob.period_end}`
    : '';

  const initialPop = populations.initial_population;
  const denominator = populations.denominator;
  const numerator = populations.numerator;
  const denomExclusion = populations.denominator_exclusion;
  const performanceRate = results?.performance_rate;

  const completedAgo = selectedJob?.completed_at ? timeAgo(selectedJob.completed_at) : null;

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <h1 className={styles.title}>Results</h1>
        {jobs.length > 0 && (
          <div className={styles.jobSelector}>
            <label htmlFor="job-select" className={styles.jobLabel}>Job:</label>
            <select
              id="job-select"
              value={selectedJobId}
              onChange={handleJobChange}
              className={styles.jobSelect}
            >
              {jobs.map(job => (
                <option key={job.id} value={job.id}>
                  {job.measure_name || job.measure_id || job.id}
                  {job.period_start ? ` (${job.period_start})` : ''}
                </option>
              ))}
            </select>
          </div>
        )}
        {selectedJobId && results && (
          <div className={styles.headerActions}>
            <button
              className={styles.btnSecondary}
              onClick={() => exportCsv(allPatients, measureName, period)}
              disabled={allPatients.length === 0}
            >
              Export CSV
            </button>
            {selectedJob && (
              <button
                className={styles.btnPrimary}
                onClick={handleRerun}
                disabled={rerunning}
                aria-busy={rerunning}
              >
                {rerunning ? 'Starting…' : 'Re-run'}
              </button>
            )}
          </div>
        )}
      </div>

      {/* Loading */}
      {loading && (
        <div role="status" aria-label="Loading results">
          <div className={styles.cardsRow}>
            {[1, 2, 3].map(i => (
              <div key={i} className={`skeleton ${styles.skeletonCard}`} />
            ))}
          </div>
          <div className={styles.tableWrapper} style={{ marginTop: 'var(--space-6)' }}>
            <table>
              <thead>
                <tr>
                  <th>Patient ID</th>
                  <th>Name</th>
                  <th>Initial Pop</th>
                  <th>Denominator</th>
                  <th>Numerator</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {[1, 2, 3, 4, 5].map(i => (
                  <tr key={i}>
                    {[1, 2, 3, 4, 5, 6].map(j => (
                      <td key={j}><div className={`skeleton ${styles.skeletonCell}`} /></td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Error */}
      {!loading && error && (
        <div className={styles.errorState} role="alert">
          <p>Error loading results</p>
          <p className={styles.errorDetail}>{error}</p>
          <button className={styles.retryBtn} onClick={loadResults}>Retry</button>
        </div>
      )}

      {/* Empty state */}
      {!loading && !error && !selectedJobId && (
        <div className={styles.emptyState}>
          <p>No results for this measure yet.</p>
          <p className={styles.emptyHint}>Run a calculation from the Jobs page to see results here.</p>
        </div>
      )}

      {/* Results content */}
      {!loading && !error && selectedJobId && results && (
        <>
          {/* Measure header */}
          {(measureName || period) && (
            <div className={styles.measureHeader}>
              <div className={styles.measureTitleRow}>
                {measureName && <h2 className={styles.measureName}>{measureName}</h2>}
                <span className={styles.completeBadge}>Complete</span>
                {completedAgo && <span className={styles.completedAgo}>Calculated {completedAgo}</span>}
              </div>
              {period && <p className={styles.period}>Period: {period}</p>}
              {selectedJob?.cdr_name && <p className={styles.cdr}>CDR: {selectedJob.cdr_name}</p>}
            </div>
          )}

          {/* Population cards */}
          <div className={styles.cardsRow}>
            <PopulationCard label="Initial Population" count={initialPop} variant="default" />
            <PopulationCard label="Numerator" count={numerator} variant="accent" />
            <PopulationCard label="Denominator" count={denominator} variant="default" />
            <PopulationCard label="Denominator Exclusions" count={denomExclusion} variant="default" />
          </div>

          {/* Performance Rate */}
          {performanceRate !== undefined && performanceRate !== null && (
            <div className={styles.performanceRate}>
              <span className={styles.perfLabel}>Performance Rate</span>
              <span className={styles.perfValue}>
                {typeof performanceRate === 'number'
                  ? `${performanceRate.toFixed(1)}%`
                  : performanceRate}
              </span>
            </div>
          )}

          {/* Patient list */}
          <div className={styles.tableWrapper}>
            <div className={styles.tableToolbar}>
              <span className={styles.patientCount}>
                {patients.length.toLocaleString()} of {allPatients.length.toLocaleString()} patients
              </span>
              <select
                className={styles.filterSelect}
                value={populationFilter}
                onChange={e => setPopulationFilter(e.target.value)}
                aria-label="Filter by population"
              >
                {FILTER_OPTIONS.map(opt => (
                  <option key={opt.value} value={opt.value}>{opt.label}</option>
                ))}
              </select>
            </div>
            <table aria-label="Patient results">
              <thead>
                <tr>
                  <th>Patient ID</th>
                  <th>Name</th>
                  <th>Initial Pop</th>
                  <th>Denominator</th>
                  <th>Numerator</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {patients.length === 0 ? (
                  <tr>
                    <td colSpan={6} className={styles.emptyRow}>
                      {allPatients.length > 0
                        ? 'No patients match this filter.'
                        : 'No individual patient results available.'}
                    </td>
                  </tr>
                ) : (
                  patients.map((patient, i) => (
                    <tr key={patient.patient_id || patient.id || i}>
                      <td className={styles.patientId}>
                        {patient.patient_id || patient.id || '--'}
                      </td>
                      <td>{patient.patient_name || patient.name || '--'}</td>
                      <td className={styles.boolCell}>
                        {patient.populations?.initial_population ? <CheckMark /> : <CrossMark />}
                      </td>
                      <td className={styles.boolCell}>
                        {patient.populations?.denominator ? <CheckMark /> : <CrossMark />}
                      </td>
                      <td className={styles.boolCell}>
                        {patient.populations?.numerator ? <CheckMark /> : <CrossMark />}
                      </td>
                      <td>
                        <button
                          className={styles.detailBtn}
                          onClick={() => handleViewPatient(patient)}
                        >
                          View Details
                        </button>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
          {/* Comparison vs expected results */}
          <ComparisonView jobId={selectedJobId} />
        </>
      )}

      {/* Patient detail slide-out */}
      {selectedPatient && (
        <PatientDetail
          result={detailLoading ? null : (patientDetail || selectedPatient)}
          onClose={closeDetail}
        />
      )}
    </div>
  );
}
