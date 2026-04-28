import React, { useState } from 'react';
import styles from './OperationOutcomeView.module.css';
import { syntaxHighlightJson } from '../utils/json';

const SEVERITY_CLASS = {
  fatal: styles.sevFatal,
  error: styles.sevError,
  warning: styles.sevWarning,
  information: styles.sevInfo,
};

export default function OperationOutcomeView({ issues = [], errorDetails = null, defaultExpanded = false }) {
  const [showRaw, setShowRaw] = useState(defaultExpanded);

  if (!issues.length && !errorDetails) return null;

  return (
    <div className={styles.root}>
      {issues.length > 0 && (
        <ul className={styles.issueList}>
          {issues.map((issue, i) => (
            <li key={i} className={styles.issueRow}>
              <span className={`${styles.severityDot} ${SEVERITY_CLASS[issue.severity] || styles.sevError}`} title={issue.severity} />
              <span className={styles.codeLabel}>{issue.code}</span>
              {issue.diagnostics && <span className={styles.diagnostics}>{issue.diagnostics}</span>}
            </li>
          ))}
        </ul>
      )}
      {errorDetails && (
        <div className={styles.details}>
          {errorDetails.hint && <p className={styles.hint}>{errorDetails.hint}</p>}
          <div className={styles.metaRow}>
            {errorDetails.status_code != null && (
              <span className={styles.metaBadge}>HTTP {errorDetails.status_code}</span>
            )}
            {errorDetails.latency_ms != null && (
              <span className={styles.metaBadge}>{errorDetails.latency_ms}ms</span>
            )}
            {errorDetails.url && (
              <span className={styles.metaUrl} title={errorDetails.url}>{errorDetails.url}</span>
            )}
          </div>
          {errorDetails.raw_outcome && (
            <div className={styles.rawBlock}>
              <button className={styles.rawToggle} onClick={() => setShowRaw(v => !v)} type="button">
                {showRaw ? '▲ Hide raw FHIR' : '▼ Show raw FHIR'}
              </button>
              {showRaw && (
                /* syntaxHighlightJson escapes all content via escapeHtml before adding span tags — safe */
                <pre
                  className={styles.fhirJson}
                  dangerouslySetInnerHTML={{ __html: syntaxHighlightJson(errorDetails.raw_outcome) }}
                />
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
