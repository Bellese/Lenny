import React, { useState, useEffect, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import styles from './ResultsPage.module.css';
import { getJobs, getResults, getResult } from '../api/client';
import PatientDetail from '../components/PatientDetail';
import ComparisonView from '../components/ComparisonView';
import Sparkline from '../components/Sparkline';
import DistBar from '../components/DistBar';
import { CheckIcon, XIcon, FilterIcon } from '../components/Icons';
import { useSearch } from '../contexts/SearchContext';

const SPARK_FALLBACK = [61.2, 62.8, 63.1, 64.0, 64.7, 65.2, 65.9, 66.1, 66.4, 66.8, 67.1, 67.4];

export default function ResultsPage() {
  const { jobId: routeJobId } = useParams();
  const navigate = useNavigate();
  const { query } = useSearch();

  const [jobs, setJobs] = useState([]);
  const [selectedJobId, setSelectedJobId] = useState(routeJobId || '');
  const [results, setResults] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selectedPatient, setSelectedPatient] = useState(null);
  const [patientDetail, setPatientDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);

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

  const selectedJob = jobs.find(j => String(j.id) === String(selectedJobId));
  const populations = results?.populations || {};
  const patients = results?.patients || [];
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

  return (
    <div className={styles.page}>
      <div className={styles.pageHeader}>
        <div>
          {measureName && <div className={styles.eyebrow}>{measureName}</div>}
          <h1 className={styles.title}>Results</h1>
          {period && <div className={styles.sub}><span className={styles.mono}>{period}</span></div>}
        </div>
        <div className={styles.headerActions}>
          {jobs.length > 0 && (
            <select className={styles.jobSelect} value={selectedJobId} onChange={handleJobChange} aria-label="Select job">
              {jobs.map(job => (
                <option key={job.id} value={job.id}>
                  {job.measure_name || job.measure_id || job.id}
                  {job.period_start ? ` (${job.period_start})` : ''}
                </option>
              ))}
            </select>
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
                <div className={styles.sparklineWrap}>
                  <Sparkline values={SPARK_FALLBACK} w={140} h={40} stroke="var(--accent)" />
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
              {ip != null && <span className={styles.tableBadge}>{ip.toLocaleString()}</span>}
              <div className={styles.tableActions}>
                <button className={styles.btnGhost}><FilterIcon /> Filter</button>
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
                    {q ? `No patients match "${q}".` : 'No individual patient results available.'}
                  </td></tr>
                ) : (
                  filteredPatients.map((patient, i) => (
                    <tr
                      key={patient.patient_id || patient.id || i}
                      className={styles.patientRow}
                      onClick={() => handleViewPatient(patient)}
                    >
                      <td><span className={styles.mono}>{patient.patient_id || patient.id || '--'}</span></td>
                      <td className={styles.patientName}>{patient.patient_name || patient.name || '--'}</td>
                      <td className={styles.popCell}>{patient.populations?.denominator ? <CheckIcon className={styles.iconOk} /> : <XIcon className={styles.iconDim} />}</td>
                      <td className={styles.popCell}>{patient.populations?.numerator ? <CheckIcon className={styles.iconOk} /> : <XIcon className={styles.iconDim} />}</td>
                      <td className={styles.popCell}>{patient.populations?.denominator_exclusion ? <CheckIcon className={styles.iconWarn} /> : <XIcon className={styles.iconDim} />}</td>
                    </tr>
                  ))
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
