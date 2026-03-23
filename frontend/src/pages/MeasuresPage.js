import React, { useState, useEffect, useCallback, useRef } from 'react';
import styles from './MeasuresPage.module.css';
import { getMeasures, uploadMeasure } from '../api/client';
import { useToast } from '../components/Toast';

function getMeasureDisplayName(measure) {
  if (measure.resource?.title) return measure.resource.title;
  if (measure.resource?.name) return measure.resource.name;
  if (measure.title) return measure.title;
  if (measure.name) return measure.name;
  return measure.id || 'Unknown Measure';
}

function getMeasureVersion(measure) {
  return measure.resource?.version || measure.version || '--';
}

function getMeasureStatus(measure) {
  return measure.resource?.status || measure.status || 'unknown';
}

function StatusBadge({ status }) {
  const normalized = (status || '').toLowerCase();
  let variant = 'default';
  let label = status;

  if (normalized === 'active' || normalized === 'ready') {
    variant = 'success';
    label = 'Active';
  } else if (normalized === 'draft') {
    variant = 'warning';
    label = 'Draft';
  } else if (normalized === 'retired') {
    variant = 'error';
    label = 'Retired';
  }

  return (
    <span className={`${styles.badge} ${styles[variant]}`} aria-label={`Status: ${label}`}>
      <span className={styles.badgeDot} aria-hidden="true" />
      {label}
    </span>
  );
}

export default function MeasuresPage() {
  const [measures, setMeasures] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef(null);
  const toast = useToast();

  const loadMeasures = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getMeasures();
      setMeasures(Array.isArray(data) ? data : data.measures || data.entry || []);
    } catch (err) {
      setError(err.message || 'Cannot reach measure engine');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadMeasures();
  }, [loadMeasures]);

  const handleUploadClick = () => {
    fileInputRef.current?.click();
  };

  const handleFileChange = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;

    // Reset file input so the same file can be re-selected
    e.target.value = '';

    setUploading(true);
    try {
      await uploadMeasure(file);
      toast.success('Measure loaded successfully');
      loadMeasures();
    } catch (err) {
      const message = err.message || 'Failed to upload measure';
      toast.error(`Upload failed: ${message}`);
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <h1 className={styles.title}>Measures</h1>
        <button
          className={styles.uploadBtn}
          onClick={handleUploadClick}
          disabled={uploading}
          aria-busy={uploading}
        >
          {uploading ? 'Uploading...' : 'Upload Measure'}
        </button>
        <input
          ref={fileInputRef}
          type="file"
          accept=".json,application/json"
          onChange={handleFileChange}
          className="sr-only"
          aria-label="Select measure bundle file"
        />
      </div>

      {/* Loading state: skeleton rows */}
      {loading && (
        <div className={styles.tableWrapper} role="status" aria-label="Loading measures">
          <table>
            <thead>
              <tr>
                <th>Measure Name</th>
                <th>Version</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {[1, 2, 3].map(i => (
                <tr key={i}>
                  <td><div className={`skeleton ${styles.skeletonCell}`} style={{ width: '60%' }} /></td>
                  <td><div className={`skeleton ${styles.skeletonCell}`} style={{ width: '40px' }} /></td>
                  <td><div className={`skeleton ${styles.skeletonCell}`} style={{ width: '60px' }} /></td>
                  <td><div className={`skeleton ${styles.skeletonCell}`} style={{ width: '80px' }} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Error state */}
      {!loading && error && (
        <div className={styles.errorState} role="alert">
          <p className={styles.errorMessage}>Cannot reach measure engine</p>
          <p className={styles.errorDetail}>{error}</p>
          <button className={styles.retryBtn} onClick={loadMeasures}>Retry</button>
        </div>
      )}

      {/* Data state */}
      {!loading && !error && (
        <div className={styles.tableWrapper}>
          <table aria-label="Loaded measures">
            <thead>
              <tr>
                <th>Measure Name</th>
                <th>Version</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {measures.length === 0 ? (
                <tr>
                  <td colSpan={4} className={styles.emptyRow}>
                    No measures loaded. Upload a measure bundle to get started.
                  </td>
                </tr>
              ) : (
                measures.map((measure, i) => (
                  <tr key={measure.id || i}>
                    <td className={styles.measureName}>{getMeasureDisplayName(measure)}</td>
                    <td className={styles.version}>{getMeasureVersion(measure)}</td>
                    <td><StatusBadge status={getMeasureStatus(measure)} /></td>
                    <td>
                      <a href="/jobs" className={styles.actionLink}>Calculate</a>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}

      <p className={styles.hint}>
        Need measure bundles? Visit the{' '}
        <a href="https://ecqi.healthit.gov/" target="_blank" rel="noopener noreferrer">
          CMS eCQI Resource Center
        </a>
        .
      </p>
    </div>
  );
}
