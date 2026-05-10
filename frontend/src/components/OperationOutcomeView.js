import React, { useState } from 'react';
import styles from './OperationOutcomeView.module.css';

const SEVERITY_CLASS = {
  fatal: styles.sevFatal,
  error: styles.sevError,
  warning: styles.sevWarning,
  information: styles.sevInfo,
};

const CODE_LABEL = {
  security: 'Auth error',
  'not-found': 'Not found',
  transient: 'Transient error',
  exception: 'Server error',
  timeout: 'Timeout',
  throttled: 'Rate limited',
  invalid: 'Invalid request',
  structure: 'Invalid structure',
  processing: 'Processing error',
};

const DIAG_TRUNCATE = 160;

function DiagnosticsText({ text }) {
  const [expanded, setExpanded] = useState(false);
  if (!text) return null;
  if (text.length <= DIAG_TRUNCATE) return <span className={styles.diagnostics}>{text}</span>;
  return (
    <span className={styles.diagnostics}>
      {expanded ? text : `${text.slice(0, DIAG_TRUNCATE)}…`}
      <button className={styles.diagToggle} onClick={() => setExpanded(v => !v)} type="button">
        {expanded ? ' show less' : ' show more'}
      </button>
    </span>
  );
}

function isRedactedUrl(url) {
  return !url || url.includes('[host]') || url === '[url-parse-error]';
}

export default function OperationOutcomeView({ issues = [], errorDetails = null, defaultExpanded = false }) {
  const [showRaw, setShowRaw] = useState(defaultExpanded);

  if (!issues.length && !errorDetails) return null;

  const visibleUrl = errorDetails?.url && !isRedactedUrl(errorDetails.url) ? errorDetails.url : null;

  return (
    <div className={styles.root}>
      {issues.length > 0 && (
        <ul className={styles.issueList}>
          {issues.map((issue, i) => {
            const codeLabel = CODE_LABEL[issue.code];
            return (
              <li key={i} className={styles.issueRow}>
                <span className={`${styles.severityDot} ${SEVERITY_CLASS[issue.severity] || styles.sevError}`} title={issue.severity} />
                {codeLabel && <span className={styles.codeLabel}>{codeLabel}</span>}
                <DiagnosticsText text={issue.diagnostics} />
              </li>
            );
          })}
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
            {visibleUrl && (
              <span className={styles.metaUrl} title={visibleUrl}>{visibleUrl}</span>
            )}
          </div>
          {errorDetails.raw_outcome && (
            <div className={styles.rawBlock}>
              <button className={styles.rawToggle} onClick={() => setShowRaw(v => !v)} type="button">
                {showRaw ? '▲ Hide raw FHIR' : '▼ Show raw FHIR'}
              </button>
              {showRaw && (
                <pre className={styles.fhirJson}>
                  {JSON.stringify(errorDetails.raw_outcome, null, 2)}
                </pre>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
