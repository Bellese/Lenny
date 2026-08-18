import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Link } from 'react-router-dom';
import styles from './MeasuresPage.module.css';
import { deleteMeasure, getMeasures, uploadMeasure } from '../api/client';
import { parseFhirError } from '../api/fhirError';
import { useToast } from '../components/Toast';
import KebabMenu from '../components/KebabMenu';
import ConfirmDialog from '../components/ConfirmDialog';
import ErrorBanner from '../components/ErrorBanner';
import { TrashIcon, PlusIcon, CheckIcon } from '../components/Icons';
import { useSearch } from '../contexts/SearchContext';
import { useConnection } from '../contexts/ConnectionContext';
import { extractCmsId, cleanMeasureName, measureDisplayLabel } from '../utils/measureFormat';

function getMeasureDisplayName(measure) {
  let name;
  if (measure.resource?.title) name = measure.resource.title;
  else if (measure.resource?.name) name = measure.resource.name;
  else if (measure.title) name = measure.title;
  else if (measure.name) name = measure.name;
  else name = measure.id || 'Unknown Measure';
  return cleanMeasureName(name);
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
  // The `mcs` block from the last successful GET /measures response — i.e.
  // where the measures ON SCREEN actually came from. This is deliberately
  // separate from the health-polled context's `mcs`: during a switch, a
  // reload, or a failed refetch the two can differ, and the subtitle must
  // describe the data on screen, not the currently-connected server —
  // otherwise we'd recreate this exact bug one layer up (#396).
  const [measuresMcs, setMeasuresMcs] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [confirm, setConfirm] = useState(null);
  const fileInputRef = useRef(null);
  const toast = useToast();
  const { query } = useSearch();
  const { mcs } = useConnection();

  const loadMeasures = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getMeasures();
      setMeasures(Array.isArray(data) ? data : data.measures || data.entry || []);
      setMeasuresMcs(Array.isArray(data) ? null : (data.mcs || null));
    } catch (err) {
      // Never render a stale list from a previous connection — the whole
      // point of this fix is that an unreachable MCS shows empty, not old data.
      setMeasures([]);
      setMeasuresMcs(null);
      const { issues, errorDetails } = parseFhirError(err.body);
      setError({ message: err.message || 'Cannot reach measure engine', issues, errorDetails });
    } finally {
      setLoading(false);
    }
  }, []);

  // Re-fetch whenever the active MCS changes (#396) — otherwise the page
  // keeps showing the previous connection's measures after activating a
  // different one in Settings.
  useEffect(() => { loadMeasures(); }, [loadMeasures, mcs.id]);

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
    const displayLabel = measureDisplayLabel(confirm.id, getMeasureDisplayName(confirm));
    const id = confirm.id;
    setConfirm(null);
    try {
      await deleteMeasure(id);
      toast.success(`Deleted ${displayLabel}`);
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
            <div className={styles.sub}>
              {visible.length} measure{visible.length !== 1 ? 's' : ''} on{' '}
              {measuresMcs?.name || mcs.name || 'the active connection'}
            </div>
          )}
        </div>
        <div className={styles.headerActions}>
          <button
            className={styles.btnPrimary}
            onClick={handleUploadClick}
            disabled={uploading || mcs.isReadOnly}
            aria-busy={uploading}
            title={mcs.isReadOnly ? `${mcs.name || 'This connection'} is read-only` : undefined}
          >
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
          <div className={styles.tableScroll}>
          <table>
            <thead>
              <tr>
                <th>ID</th><th className={styles.measureCell}>Measure</th><th>Version</th><th>Status</th><th style={{ textAlign: 'right' }}>Actions</th>
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
        </div>
      )}

      {!loading && error && (
        <div className={styles.errorState}>
          <ErrorBanner
            title={`Cannot reach ${mcs.name || 'the measure engine'}`}
            message={error.message}
            issues={error.issues}
            errorDetails={error.errorDetails}
          />
          <button className={styles.retryBtn} onClick={loadMeasures}>Retry</button>
        </div>
      )}

      {!loading && !error && (
        <div className={styles.card}>
          <div className={styles.tableScroll}>
          <table aria-label="Loaded measures">
            <thead>
              <tr>
                <th style={{ width: 120 }}>ID</th>
                <th className={styles.measureCell}>Measure</th>
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
                    <td data-label="ID"><span className={styles.mono}>{extractCmsId(measure.id) || measure.id || '--'}</span></td>
                    <td data-label="Measure" className={`${styles.measureName} ${styles.measureCell}`}>{getMeasureDisplayName(measure)}</td>
                    <td data-label="Version" className={styles.mono} style={{ color: 'var(--text-muted)' }}>{getMeasureVersion(measure)}</td>
                    <td data-label="Status"><StatusBadge status={getMeasureStatus(measure)} /></td>
                    <td data-label="Actions">
                      <div className={styles.actionGroup}>
                        <Link to={`/jobs?newCalc=${encodeURIComponent(measure.id || '')}`} className={styles.calcBtn}>Calculate</Link>
                        <KebabMenu items={[
                          { divider: true },
                          {
                            label: 'Delete permanently',
                            icon: <TrashIcon />,
                            tone: 'destructive',
                            disabled: !measure.id || mcs.isReadOnly,
                            title: mcs.isReadOnly ? `${mcs.name || 'This connection'} is read-only` : undefined,
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
        </div>
      )}

      <ConfirmDialog
        open={!!confirm}
        title={`Delete ${confirm?.id}?`}
        body={<>This removes <strong>{confirm ? measureDisplayLabel(confirm.id, getMeasureDisplayName(confirm)) : ''}</strong> from Lenny. Existing job results are preserved, but you won't be able to re-run without re-uploading the bundle.</>}
        confirmLabel="Delete permanently"
        tone="destructive"
        onCancel={() => setConfirm(null)}
        onConfirm={handleDeleteConfirmed}
      />
    </div>
  );
}
