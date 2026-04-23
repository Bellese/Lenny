import React, { useState, useEffect, useCallback } from 'react';
import styles from './PatientDetail.module.css';
import { getEvaluatedResources } from '../api/client';

function CheckIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <circle cx="8" cy="8" r="7" fill="var(--color-success-light)" stroke="var(--color-success)" strokeWidth="1.5" />
      <path d="M5 8l2 2 4-4" stroke="var(--color-success)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function CrossIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <circle cx="8" cy="8" r="7" fill="var(--color-error-light)" stroke="var(--color-error)" strokeWidth="1.5" />
      <path d="M5.5 5.5l5 5M10.5 5.5l-5 5" stroke="var(--color-error)" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

function translateResource(resource) {
  if (!resource) return null;
  const type = resource.resourceType;

  if (type === 'Observation') {
    const code = resource.code?.coding?.[0]?.display || resource.code?.text || 'Observation';
    const value = resource.valueQuantity
      ? `${resource.valueQuantity.value}${resource.valueQuantity.unit ? ' ' + resource.valueQuantity.unit : ''}`
      : resource.valueCodeableConcept?.text || resource.valueString || '';
    const date = resource.effectiveDateTime || resource.issued || '';
    const dateStr = date ? ` (${new Date(date).toLocaleDateString()})` : '';
    return `${code}: ${value}${dateStr}`;
  }

  if (type === 'Condition') {
    const name = resource.code?.coding?.[0]?.display || resource.code?.text || 'Condition';
    const status = resource.clinicalStatus?.coding?.[0]?.code || '';
    const onset = resource.onsetDateTime || '';
    const onsetStr = onset ? ` since ${new Date(onset).toLocaleDateString()}` : '';
    return `Diagnosis: ${name} (${status}${onsetStr})`;
  }

  if (type === 'Procedure') {
    const name = resource.code?.coding?.[0]?.display || resource.code?.text || 'Procedure';
    const date = resource.performedDateTime || resource.performedPeriod?.start || '';
    const dateStr = date ? ` (${new Date(date).toLocaleDateString()})` : '';
    return `Procedure: ${name}${dateStr}`;
  }

  if (type === 'Encounter') {
    const encType = resource.type?.[0]?.coding?.[0]?.display || resource.type?.[0]?.text || 'Encounter';
    const date = resource.period?.start || '';
    const dateStr = date ? ` (${new Date(date).toLocaleDateString()})` : '';
    return `Encounter: ${encType}${dateStr}`;
  }

  if (type === 'MedicationRequest') {
    const med = resource.medicationCodeableConcept?.coding?.[0]?.display
      || resource.medicationCodeableConcept?.text
      || 'Medication';
    const date = resource.authoredOn || '';
    const dateStr = date ? ` (${new Date(date).toLocaleDateString()})` : '';
    return `Medication: ${med}${dateStr}`;
  }

  // Fallback
  const display = resource.code?.coding?.[0]?.display || resource.code?.text || type || 'Resource';
  return `${type}: ${display}`;
}

function syntaxHighlightJson(json) {
  const str = typeof json === 'string' ? json : JSON.stringify(json, null, 2);
  return str.replace(
    /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/g,
    (match) => {
      let cls = 'number';
      if (/^"/.test(match)) {
        cls = /:$/.test(match) ? 'key' : 'string';
      } else if (/true|false/.test(match)) {
        cls = 'boolean';
      } else if (/null/.test(match)) {
        cls = 'null';
      }
      return `<span class="${cls}">${match}</span>`;
    }
  );
}

export default function PatientDetail({ result, onClose }) {
  const [resources, setResources] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [showRawFhir, setShowRawFhir] = useState(false);
  const [expandedResources, setExpandedResources] = useState({});

  const loadResources = useCallback(async () => {
    if (!result?.id) return;
    setLoading(true);
    setError(null);
    try {
      const data = await getEvaluatedResources(result.id);
      setResources(data);
    } catch (err) {
      setError(err.message || 'Failed to load clinical data');
    } finally {
      setLoading(false);
    }
  }, [result?.id]);

  useEffect(() => {
    loadResources();
  }, [loadResources]);

  // Escape to close
  useEffect(() => {
    function handleKeyDown(e) {
      if (e.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  const toggleResourceExpand = (index) => {
    setExpandedResources(prev => ({ ...prev, [index]: !prev[index] }));
  };

  if (!result) return null;

  const populations = [
    { key: 'initial_population', label: 'Initial Population' },
    { key: 'denominator', label: 'Denominator' },
    { key: 'denominator_exclusion', label: 'Denominator Exclusion' },
    { key: 'numerator', label: 'Numerator' },
    { key: 'numerator_exclusion', label: 'Numerator Exclusion' },
  ];

  const patientName = result.patient_name || result.patient_id || 'Unknown Patient';
  const evaluationError = result.error_message || result.populations?.error_message;

  return (
    <div className={styles.overlay} onClick={onClose} role="dialog" aria-label={`Patient details for ${patientName}`}>
      <div className={styles.panel} onClick={e => e.stopPropagation()}>
        <div className={styles.header}>
          <h2 className={styles.title}>{patientName}</h2>
          <span className={styles.patientId}>{result.patient_id}</span>
          <button className={styles.closeBtn} onClick={onClose} aria-label="Close patient details">
            &times;
          </button>
        </div>

        {/* Population membership */}
        <section className={styles.section}>
          <h3 className={styles.sectionTitle}>Population Membership</h3>
          {evaluationError && (
            <div className={styles.error}>
              <p>Evaluation failed: {evaluationError}</p>
            </div>
          )}
          <ul className={styles.populationList}>
            {populations.map(pop => {
              const inPop = result[pop.key] ?? result.populations?.[pop.key];
              if (inPop === undefined) return null;
              return (
                <li key={pop.key} className={styles.populationItem}>
                  {inPop ? <CheckIcon /> : <CrossIcon />}
                  <span>{pop.label}</span>
                  <span className={styles.popStatus}>
                    {inPop ? 'Yes' : 'No'}
                  </span>
                </li>
              );
            })}
          </ul>
        </section>

        {/* Evaluated resources */}
        <section className={styles.section}>
          <div className={styles.sectionHeader}>
            <h3 className={styles.sectionTitle}>Evaluated Resources</h3>
            <button
              className={styles.toggleBtn}
              onClick={() => setShowRawFhir(!showRawFhir)}
              aria-pressed={showRawFhir}
            >
              {showRawFhir ? 'Show clinical view' : 'Show raw FHIR'}
            </button>
          </div>

          {loading && (
            <div className={styles.skeletons}>
              {[1, 2, 3, 4].map(i => (
                <div key={i} className={`skeleton ${styles.skeletonRow}`} />
              ))}
            </div>
          )}

          {error && (
            <div className={styles.error}>
              <p>Clinical data no longer available &mdash; cleared when a new calculation started.</p>
              <button className={styles.retryBtn} onClick={loadResources}>Retry</button>
            </div>
          )}

          {!loading && !error && resources && (
            <>
              {!showRawFhir ? (
                <ul className={styles.resourceList}>
                  {(Array.isArray(resources) ? resources : resources.resources || []).map((res, i) => (
                    <li key={i} className={styles.resourceItem}>
                      {translateResource(res)}
                    </li>
                  ))}
                  {(Array.isArray(resources) ? resources : resources.resources || []).length === 0 && (
                    <li className={styles.resourceItem}>No evaluated resources available.</li>
                  )}
                </ul>
              ) : (
                <div className={styles.rawFhir}>
                  {(Array.isArray(resources) ? resources : resources.resources || []).map((res, i) => (
                    <div key={i} className={styles.fhirBlock}>
                      <button
                        className={styles.fhirToggle}
                        onClick={() => toggleResourceExpand(i)}
                        aria-expanded={!!expandedResources[i]}
                      >
                        <span>{expandedResources[i] ? '\u25BC' : '\u25B6'}</span>
                        <span>{res.resourceType || 'Resource'}/{res.id || i}</span>
                      </button>
                      {expandedResources[i] && (
                        <pre
                          className={styles.fhirJson}
                          dangerouslySetInnerHTML={{
                            __html: syntaxHighlightJson(res),
                          }}
                        />
                      )}
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </section>
      </div>
    </div>
  );
}
