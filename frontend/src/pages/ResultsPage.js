import React, { useState, useEffect, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import styles from './ResultsPage.module.css';
import { getJobs, getResults, getResult, createJob } from '../api/client';
import { useToast } from '../components/Toast';
import PatientDetail from '../components/PatientDetail';
import ComparisonView from '../components/ComparisonView';
import DistBar from '../components/DistBar';
import { CheckIcon, XIcon } from '../components/Icons';
import { useSearch } from '../contexts/SearchContext';
import { extractCmsId, cleanMeasureName, measureOptionLabel, measureDisplayLabel } from '../utils/measureFormat';

function timeAgo(dateStr) {
  if (!dateStr) return null;
  const diff = Math.floor((Date.now() - new Date(dateStr)) / 1000);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function exportCsv(patients, measureId, measureName, period) {
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
  const cmsId = extractCmsId(measureId);
  const fileLabel = cmsId ? `${cmsId}-${cleanMeasureName(measureName || '')}` : (measureName || 'measure');
  a.download = `results-${fileLabel}-${period || 'period'}.csv`.replace(/\s+/g, '-');
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
  const { query } = useSearch();

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
          if (String(selectedJobId) !== String(preferredId)) setSelectedJobId(String(preferredId));
          return;
        }
        const fallbackId = completed[0] ? String(completed[0].id) : '';
        if (String(selectedJobId) !== fallbackId) setSelectedJobId(fallbackId);
        if (routeJobId && String(routeJobId) !== fallbackId) {
          navigate(fallbackId ? `/results/${fallbackId}` : '/results', { replace: true });
        }
      } catch { /* non-blocking */ }
    }
    loadJobs();
  }, [navigate, routeJobId, selectedJobId]);

  const loadResults = useCallback(async () => {
    if (!selectedJobId) { setResults(null); setLoading(false); return; }
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

  useEffect(() => { loadResults(); }, [loadResults]);

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
  const populations = results?.populations || {};
  const allPatients = results?.patients || [];
  const patients = applyFilter(allPatients, populationFilter);
  const measureName = results?.measure_name || selectedJob?.measure_name || selectedJob?.measure_id || '';
  const period = selectedJob?.period_start && selectedJob?.period_end
    ? `${selectedJob.period_start} → ${selectedJob.period_end}` : '';

  const ip = populations.initial_population;
  const den = populations.denominator;
  const num = populations.numerator;
  const exc = populations.denominator_exclusion;
  const perfRate = results?.performance_rate;

  const q = query.trim().toLowerCase();
  const filteredPatients = patients.filter(p => {
    if (!q) return true;
    const name = (p.patient_name || p.name || '').toLowerCase();
    const id = (p.patient_id || p.id || '').toLowerCase();
    return name.includes(q) || id.includes(q);
  });

  const completedAgo = selectedJob?.completed_at ? timeAgo(selectedJob.completed_at) : null;

  return (
    <div className={styles.page}>
      <div className={styles.pageHeader}>
        <div>
          {measureName && <div className={styles.eyebrow}>{measureDisplayLabel(selectedJob?.measure_id, measureName)}</div>}
          <h1 className={styles.title}>Results</h1>
          {period && <div className={styles.sub}><span className={styles.mono}>{period}</span></div>}
          {completedAgo && (
            <div className={styles.sub}>
              <span className={styles.completeBadge}>Complete</span>{' '}Calculated {completedAgo}
            </div>
          )}
        </div>
        <div className={styles.headerActions}>
          {jobs.length > 0 && (
            <div className={styles.jobSelectGroup}>
              <label htmlFor="job-run-select" className={styles.jobSelectLabel}>Job run</label>
              <select id="job-run-select" className={styles.jobSelect} value={selectedJobId} onChange={handleJobChange}>
                {jobs.map(job => (
                  <option key={job.id} value={job.id}>
                    {measureOptionLabel(job.measure_id, job.measure_name)}
                    {job.period_start ? ` (${job.period_start})` : ''}
                  </option>
                ))}
              </select>
            </div>
          )}
          {selectedJobId && results && (
            <>
              <button
                className={styles.btnGhost}
                onClick={() => exportCsv(allPatients, selectedJob?.measure_id, measureName, period)}
                disabled={allPatients.length === 0}
              >
                Export CSV
              </button>
              {selectedJob && (
                <button
                  className={styles.btnAccent}
                  onClick={handleRerun}
                  disabled={rerunning}
                  aria-busy={rerunning}
                >
                  {rerunning ? 'Starting…' : 'Re-run'}
                </button>
              )}
            </>
          )}
        </div>
      </div>

      {loading && (
        <div role="status" aria-label="Loading results">
          <div className={styles.cardsRow}>
            {[1, 2].map(i => <div key={i} className={`skeleton ${styles.skeletonCard}`} />)}
          </div>
        </div>
      )}

      {!loading && error && (
        <div className={styles.errorState} role="alert">
          <p>Error loading results</p>
          <p className={styles.errorDetail}>{error}</p>
          <button className={styles.retryBtn} onClick={loadResults}>Retry</button>
        </div>
      )}

      {!loading && !error && !selectedJobId && (
        <div className={styles.emptyState}>
          <p>No results yet.</p>
          <p className={styles.emptyHint}>Run a calculation from the Jobs page.</p>
        </div>
      )}

      {!loading && !error && selectedJobId && results && (
        <>
          {/* Top stat cards */}
          <div className={styles.cardsRow}>
            {/* Performance rate card */}
            <div className={styles.card}>
              <div className={styles.cardTopRow}>
                <div>
                  <div className={styles.cardEyebrow}>Performance rate</div>
                  <div className={styles.cardSub}>Numerator ÷ (Denominator − Exclusions)</div>
                </div>
              </div>
              <div className={styles.rateRow}>
                {perfRate !== undefined && perfRate !== null ? (
                  <div className={styles.rateNum}>
                    {typeof perfRate === 'number' ? perfRate.toFixed(1) : perfRate}
                    <span className={styles.ratePct}>%</span>
                  </div>
                ) : (
                  <div className={styles.rateNum}>—</div>
                )}
              </div>
            </div>

            {/* Populations card */}
            <div className={styles.card}>
              <div className={styles.cardTopRow}>
                <div className={styles.cardEyebrow}>Populations</div>
                {ip != null && <div className={styles.cardSub}>of {ip.toLocaleString()} IP</div>}
              </div>
              {ip != null && (
                <>
                  <div className={styles.distBarWrap}>
                    <DistBar ip={ip} den={den || 0} num={num || 0} exc={exc || 0} height={6} />
                  </div>
                  <div className={styles.popGrid}>
                    {[
                      ['Initial Pop.', ip, 'text'],
                      ['Denominator', den, 'text'],
                      ['Numerator', num, 'accent'],
                      ['Exclusions', exc, 'warn'],
                    ].map(([label, val, tone]) => (
                      <div key={label} className={styles.popRow}>
                        <span className={styles.popLabel}>{label}</span>
                        <span className={styles.popVal} data-tone={tone}>
                          {val != null ? val.toLocaleString() : '--'}
                        </span>
                      </div>
                    ))}
                  </div>
                </>
              )}
            </div>
          </div>

          {/* Patients table */}
          <div className={styles.card}>
            <div className={styles.tableHeader}>
              <span className={styles.tableTitle}>Patients</span>
              <span className={styles.tableBadge}>
                {filteredPatients.length !== allPatients.length
                  ? `${filteredPatients.length} / ${allPatients.length}`
                  : allPatients.length.toLocaleString()}
              </span>
              <div className={styles.tableActions}>
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
            </div>
            <table aria-label="Patient results">
              <thead>
                <tr>
                  <th style={{ width: 130 }}>Patient ID</th>
                  <th>Name</th>
                  <th style={{ width: 80, textAlign: 'center' }}>Denom</th>
                  <th style={{ width: 80, textAlign: 'center' }}>Numer</th>
                  <th style={{ width: 80, textAlign: 'center' }}>Excl.</th>
                </tr>
              </thead>
              <tbody>
                {filteredPatients.length === 0 ? (
                  <tr><td colSpan={5} className={styles.emptyRow}>
                    {q
                      ? `No patients match "${q}".`
                      : populationFilter !== 'all'
                        ? 'No patients match this filter.'
                        : 'No individual patient results available.'}
                  </td></tr>
                ) : (
                  filteredPatients.map((patient, i) => {
                    const isError = patient.populations?.error === true || patient.status === 'error';
                    const phase = patient.error_phase;
                    const phaseLabel = phase === 'gather' ? 'gather failed'
                      : phase === 'gather_partial' ? 'partial data'
                      : phase === 'evaluate' ? 'eval failed'
                      : isError ? 'error' : null;
                    return (
                      <tr
                        key={patient.patient_id || patient.id || i}
                        className={`${styles.patientRow} ${isError ? styles.patientRowError : ''}`}
                        onClick={() => handleViewPatient(patient)}
                      >
                        <td data-label="Patient ID"><span className={styles.mono}>{patient.patient_id || patient.id || '--'}</span></td>
                        <td data-label="Name" className={styles.patientName}>
                          {patient.patient_name || patient.name || '--'}
                          {isError && <span className={styles.errorBadge}>Error</span>}
                          {isError && phaseLabel && <span className={styles.errorPhaseLabel}>{phaseLabel}</span>}
                          {!isError && phase === 'gather_partial' && <span className={styles.warnBadge}>Partial data</span>}
                        </td>
                        {isError ? (
                          <>
                            <td data-label="Denom" className={styles.popCell} colSpan={3}>
                              <span className={styles.errorPhaseLabel} title={patient.error_message || ''}>
                                {patient.error_message ? patient.error_message.slice(0, 60) : 'See details'}
                              </span>
                            </td>
                          </>
                        ) : (
                          <>
                            <td data-label="Denom" className={styles.popCell}>{patient.populations?.denominator ? <CheckIcon className={styles.iconOk} /> : <XIcon className={styles.iconDim} />}</td>
                            <td data-label="Numer" className={styles.popCell}>{patient.populations?.numerator ? <CheckIcon className={styles.iconOk} /> : <XIcon className={styles.iconDim} />}</td>
                            <td data-label="Excl." className={styles.popCell}>{patient.populations?.denominator_exclusion ? <CheckIcon className={styles.iconWarn} /> : <XIcon className={styles.iconDim} />}</td>
                          </>
                        )}
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>

          <ComparisonView jobId={selectedJobId} />
        </>
      )}

      {selectedPatient && (
        <PatientDetail
          result={detailLoading ? null : (patientDetail || selectedPatient)}
          onClose={closeDetail}
        />
      )}
    </div>
  );
}
