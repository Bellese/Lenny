import React from 'react';
import styles from './ErrorBanner.module.css';
import OperationOutcomeView from './OperationOutcomeView';

export default function ErrorBanner({ title, message, issues, errorDetails, onDismiss }) {
  if (!message && !issues?.length && !errorDetails) return null;

  return (
    <div className={styles.banner} role="alert">
      <div className={styles.content}>
        {title && <p className={styles.title}>{title}</p>}
        {message && <p className={styles.message}>{message}</p>}
        {(issues?.length || errorDetails) && (
          <OperationOutcomeView issues={issues} errorDetails={errorDetails} />
        )}
      </div>
      {onDismiss && (
        <button className={styles.dismiss} onClick={onDismiss} aria-label="Dismiss" type="button">
          &#x2715;
        </button>
      )}
    </div>
  );
}
