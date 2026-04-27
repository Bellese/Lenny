import React, { useState, useEffect, useCallback } from 'react';
import styles from './PatientDetail.module.css';
import { getEvaluatedResources } from '../api/client';
import { XIcon, CheckIcon } from './Icons';

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
    return { label: code, value: `${value}${dateStr}` };
  }
  if (type === 'Condition') {
    const name = resource.code?.coding?.[0]?.display || resource.code?.text || 'Condition';
    const status = resource.clinicalStatus?.coding?.[0]?.code || '';
    const onset = resource.onsetDateTime || '';
    const onsetStr = onset ? ` since ${new Date(onset).toLocaleDateString()}` : '';
    return { label: 'Diagnosis', value: `${name} (${status}${onsetStr})` };
  }
  if (type === 'Procedure') {
    const name = resource.code?.coding?.[0]?.display || resource.code?.text || 'Procedure';
    const date = resource.performedDateTime || resource.performedPeriod?.start || '';
    const dateStr = date ? ` (${new Date(date).toLocaleDateString()})` : '';
    return { label: 'Procedure', value: `${name}${dateStr}` };
  }
  if (type === 'Encounter') {
    const encType = resource.type?.[0]?.coding?.[0]?.display || resource.type?.[0]?.text || 'Encounter';
    const date = resource.period?.start || '';
    const dateStr = date ? ` (${new Date(date).toLocaleDateString()})` : '';
    return { label: 'Encounter', value: `${encType}${dateStr}` };
  }
  if (type === 'MedicationRequest') {
    const med = resource.medicationCodeableConcept?.coding?.[0]?.display || resource.medicationCodeableConcept?.text || 'Medication';
    const date = resource.authoredOn || '';
    const dateStr = date ? ` (${new Date(date).toLocaleDateString()})` : '';
    return { label: 'Medication', value: `${med}${dateStr}` };
  }
  const display = resource.code?.coding?.[0]?.display || resource.code?.text || type || 'Resource';
  return { label: type || 'Resource', value: display };
}

function escapeHtml(str) {
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function syntaxHighlightJson(json) {
  const str = typeof json === 'string' ? json : JSON.stringify(json, null, 2);
  return str.replace(
    /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/g,
    (match) => {
      let cls = 'number';
      if (/^"/.test(match)) cls = /:$/.test(match) ? 'key' : 'string';
      else if (/true|false/.test(match)) cls = 'boolean';
      else if (/null/.test(match)) cls = 'null';
      return `<span class="${cls}">${escapeHtml(match)}</span>`;
    }
  );
}

const POPULATIONS = [
  { key: 'initial_population', label: 'Initial population' },
  { key: 'denominator', label: 'Denominator' },
  { key: 'denominator_exclusion', label: 'Exclusion' },
  { key: 'numerator', label: 'Numerator' },
  { key: 'numerator_exclusion', label: 'Numerator exclusion' },
];

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

  useEffect(() => { loadResources(); }, [loadResources]);

  useEffect(() => {
    const h = (e) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', h);
    return () => document.removeEventListener('keydown', h);
  }, [onClose]);

  const toggleResourceExpand = (index) => {
    setExpandedResources(prev => ({ ...prev, [index]: !prev[index] }));
  };

  if (!result) return null;

  const patientName = result.patient_name || result.patient_id || 'Unknown Patient';
  const activePops = POPULATIONS.filter(p => result[p.key] !== undefined);
  const resourceList = Array.isArray(resources) ? resources : resources?.resources || [];

  return (
    <div className={styles.overlay} onClick={onClose} role="dialog" aria-modal="true" aria-label={`Patient details: ${patientName}`}>
      <div className={styles.drawer} onClick={(e) => e.stopPropagation()}>
        <div className={styles.drawerHeader}>
          <div className={styles.headerTop}>
            <span className={styles.patientId}>{result.patient_id}</span>
            <button className={styles.closeBtn} onClick={onClose} aria-label="Close"><XIcon /></button>
          </div>
          <div className={styles.patientName}>{patientName}</div>
          <div className={styles.badges}>
            {activePops.map(p => {
              if (!result[p.key]) return null;
              const tone = p.key === 'numerator' ? 'info'
                : p.key === 'denominator_exclusion' || p.key === 'numerator_exclusion' ? 'warn'
                : 'ok';
              return (
                <span key={p.key} className={`${styles.badge} ${styles['badge_' + tone]}`}>
                  {p.label}
                </span>
              );
            })}
          </div>
        </div>

        <div className={styles.drawerBody}>
          <section className={styles.section}>
            <h3 className={styles.sectionTitle}>Population membership</h3>
            <div className={styles.popList}>
              {activePops.map(pop => {
                const inPop = result[pop.key];
                return (
                  <div key={pop.key} className={styles.popItem}>
                    <span className={inPop ? styles.iconOk : styles.iconNo}>
                      {inPop ? <CheckIcon /> : <XIcon />}
                    </span>
                    <span className={styles.popLabel}>{pop.label}</span>
                    <span className={`${styles.popStatus} ${inPop ? styles.popStatusYes : styles.popStatusNo}`}>
                      {inPop ? 'Yes' : 'No'}
                    </span>
                  </div>
                );
              })}
            </div>
          </section>

          <section className={styles.section}>
            <div className={styles.sectionHeader}>
              <h3 className={styles.sectionTitle}>Evaluated resources</h3>
              <button className={styles.toggleBtn} onClick={() => setShowRawFhir(!showRawFhir)} aria-pressed={showRawFhir}>
                {showRawFhir ? 'Clinical view' : 'Raw FHIR'}
              </button>
            </div>

            {loading && (
              <div className={styles.skeletons}>
                {[1, 2, 3, 4].map(i => <div key={i} className={'skeleton ' + styles.skeletonRow} />)}
              </div>
            )}

            {error && (
              <div className={styles.errorMsg}>
                <p>Clinical data unavailable.</p>
                <button className={styles.retryBtn} onClick={loadResources}>Retry</button>
              </div>
            )}

            {!loading && !error && resources && (
              !showRawFhir ? (
                <div className={styles.factGrid}>
                  {resourceList.map((res, i) => {
                    const t = translateResource(res);
                    if (!t) return null;
                    return (
                      <div key={i} className={styles.factRow}>
                        <span className={styles.factLabel}>{t.label}</span>
                        <span className={styles.factValue}>{t.value}</span>
                      </div>
                    );
                  })}
                  {resourceList.length === 0 && <p className={styles.emptyMsg}>No evaluated resources available.</p>}
                </div>
              ) : (
                <div className={styles.rawFhir}>
                  {resourceList.map((res, i) => (
                    <div key={i} className={styles.fhirBlock}>
                      <button className={styles.fhirToggle} onClick={() => toggleResourceExpand(i)} aria-expanded={!!expandedResources[i]}>
                        <span>{expandedResources[i] ? '▾' : '▸'}</span>
                        <span>{res.resourceType || 'Resource'}/{res.id || i}</span>
                      </button>
                      {expandedResources[i] && (
                        <pre className={styles.fhirJson} dangerouslySetInnerHTML={{ __html: syntaxHighlightJson(res) }} />
                      )}
                    </div>
                  ))}
                </div>
              )
            )}
          </section>
        </div>
      </div>
    </div>
  );
}
