import React, { useState, useEffect, useCallback, useRef } from 'react';
import styles from './MeasuresPage.module.css';
import { deleteMeasure, getMeasures, uploadMeasure } from '../api/client';
import { useToast } from '../components/Toast';
import KebabMenu from '../components/KebabMenu';
import ConfirmDialog from '../components/ConfirmDialog';
import { TrashIcon, PlusIcon, CheckIcon } from '../components/Icons';
import { useSearch } from '../contexts/SearchContext';

function getMeasureDisplayName(measure) {
  let name;
  if (measure.resource?.title) name = measure.resource.title;
  else if (measure.resource?.name) name = measure.resource.name;
  else if (measure.title) name = measure.title;
  else if (measure.name) name = measure.name;
  else name = measure.id || 'Unknown Measure';
  return name.replace(/\s+FHIR\s*$/, '');
}

function getMeasureVersion(measure) {
  return measure.resource?.version || measure.version || '--';
}

function getMeasureStatus(measure) {
  return measure.resource?.status || measure.status || 'unknown';
}

function StatusBadge({ status }) {
  const normalized = (status || '').toLowerCase();
  if (normalized === 'active' || normalized === 'ready') {
    return (
      <span className={`${styles.badge} ${styles.badgeOk}`}>
        <CheckIcon className={styles.badgeIcon} /> Active
      </span>
    );
  }
  if (normalized === 'draft') {
    return <span className={`${styles.badge} ${styles.badgeDraft}`}>Draft</span>;
  }
  if (normalized === 'retired') {
    return <span className={`${styles.badge} ${styles.badgeRetired}`}>Retired</span>;
  }
  return <span className={styles.badge}>{status}</span>;
}

export default function MeasuresPage() {
  const [measures, setMeasures] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [confirm, setConfirm] = useState(null);
  const fileInputRef = useRef(null);
  const toast = useToast();
  const { query } = useSearch();

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

  useEffect(() => { loadMeasures(); }, [loadMeasures]);

  const handleUploadClick = () => fileInputRef.current?.click();

  const handleFileChange = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    e.target.value = '';
    setUploading(true);
    try {
      await uploadMeasure(file);
      toast.success('Measure loaded successfully');
      loadMeasures();
    } catch (err) {
      toast.error(`Upload failed: ${err.message || 'Failed to upload measure'}`);
    } finally {
      setUploading(false);
    }
  };

  const confirmDelete = (measure) => setConfirm(measure);

  const handleDeleteConfirmed = async () => {
    if (!confirm?.id) return;
    const measureName = getMeasureDisplayName(confirm);
    const id = confirm.id;
    setConfirm(null);
    try {
      await deleteMeasure(id);
      toast.success(`Deleted ${measureName}`);
      await loadMeasures();
    } catch (err) {
      toast.error(`Delete failed: ${err.message || 'Failed to delete measure'}`);
    }
  };

  const q = query.trim().toLowerCase();
  const visible = measures.filter(m => {
    if (!q) return true;
    const name = getMeasureDisplayName(m).toLowerCase();
    const id = (m.id || '').toLowerCase();
    return name.includes(q) || id.includes(q);
  });

  return (
    <div className={styles.page}>
      <div className={styles.pageHeader}>
        <div>
          <div className={styles.eyebrow}>Library</div>
          <h1 className={styles.title}>Measures</h1>
          {!loading && !error && (
            <div className={styles.sub}>{visible.length} measure{visible.length !== 1 ? 's' : ''}</div>
          )}
        </div>
        <div className={styles.headerActions}>
          <button className={styles.btnPrimary} onClick={handleUploadClick} disabled={uploading} aria-busy={uploading}>
            <PlusIcon /> {uploading ? 'Uploading…' : 'Upload bundle'}
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
      </div>

      {loading && (
        <div className={styles.card} role="status" aria-label="Loading measures">
          <table>
            <thead>
              <tr>
                <th>ID</th><th>Measure</th><th>Version</th><th>Status</th><th style={{ textAlign: 'right' }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {[1, 2, 3].map(i => (
                <tr key={i}>
                  {[90, 200, 60, 80, 100].map((w, j) => (
                    <td key={j}><div className="skeleton" style={{ height: 14, width: w }} /></td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!loading && error && (
        <div className={styles.errorState} role="alert">
          <p className={styles.errorMessage}>Cannot reach measure engine</p>
          <p className={styles.errorDetail}>{error}</p>
          <button className={styles.retryBtn} onClick={loadMeasures}>Retry</button>
        </div>
      )}

      {!loading && !error && (
        <div className={styles.card}>
          <table aria-label="Loaded measures">
            <thead>
              <tr>
                <th style={{ width: 120 }}>ID</th>
                <th>Measure</th>
                <th style={{ width: 90 }}>Version</th>
                <th style={{ width: 100 }}>Status</th>
                <th style={{ width: 100, textAlign: 'right' }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {visible.length === 0 ? (
                <tr>
                  <td colSpan={5} className={styles.emptyRow}>
                    {q ? `No measures match "${q}".` : 'No measures loaded. Upload a measure bundle to get started.'}
                  </td>
                </tr>
              ) : (
                visible.map((measure, i) => (
                  <tr key={measure.id || i} className={styles.row}>
                    <td data-label="ID"><span className={styles.mono}>{measure.id || '--'}</span></td>
                    <td data-label="Measure" className={styles.measureName}>{getMeasureDisplayName(measure)}</td>
                    <td data-label="Version" className={styles.mono} style={{ color: 'var(--text-muted)' }}>{getMeasureVersion(measure)}</td>
                    <td data-label="Status"><StatusBadge status={getMeasureStatus(measure)} /></td>
                    <td data-label="Actions">
                      <div className={styles.actionGroup}>
                        <a href="/jobs" className={styles.calcBtn}>Calculate</a>
                        <KebabMenu items={[
                          { divider: true },
                          {
                            label: 'Delete permanently',
                            icon: <TrashIcon />,
                            tone: 'destructive',
                            disabled: !measure.id,
                            onClick: () => confirmDelete(measure),
                          },
                        ]} />
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}

      <ConfirmDialog
        open={!!confirm}
        title={`Delete ${confirm?.id}?`}
        body={<>This removes <strong>{confirm ? getMeasureDisplayName(confirm) : ''}</strong> from MCT2. Existing job results are preserved, but you won't be able to re-run without re-uploading the bundle.</>}
        confirmLabel="Delete permanently"
        tone="destructive"
        onCancel={() => setConfirm(null)}
        onConfirm={handleDeleteConfirmed}
      />
    </div>
  );
}
