import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Link } from 'react-router-dom';
import styles from './MeasuresPage.module.css';
import { deleteMeasure, getMeasures, refreshMeasureReadiness, uploadMeasure } from '../api/client';
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

// Readiness answers "can the active MCS actually evaluate this measure?".
// Deliberately separate from StatusBadge above, which renders the FHIR
// Measure.status (active/draft/retired) — a different question entirely.
const READINESS_LABELS = {
  ready: 'Ready',
  not_ready: 'Not ready',
  checking: 'Checking…',
  unknown: 'Not checked',
};

// `unknown` means "not checked, or the check could not complete" — a timeout,
// a refused credential, a restart mid-sweep. All three record WHY, and hiding
// that behind a bare "Not checked" is what the design spec promised against.
// So an `unknown` WITH an error gets the same expandable affordance as
// not_ready; an `unknown` with nothing to say stays a plain, silent badge.
// This is the single source of truth for "is there anything to expand" — the
// badge and the detail row must never disagree, or a row can be left expanded
// over an empty grey panel.
function hasReadinessDetail(readiness) {
  if (!readiness) return false;
  if (readiness.state === 'not_ready') return true;
  return readiness.state === 'unknown' && !!readiness.error;
}

function ReadinessBadge({ readiness, expanded, onToggle, measureName }) {
  const state = readiness?.state || 'unknown';
  const label = READINESS_LABELS[state] || READINESS_LABELS.unknown;

  // Readiness always renders on `.readinessBadge` (an outline/ghost
  // treatment) rather than Status's filled `.badge` modifiers, so the two
  // columns never read as the same kind of thing even before the label is
  // read. Only ready/checking add a tone modifier on top of that shared
  // outline — anything else (unknown) renders the neutral outline alone.
  if (state === 'ready') {
    return <span className={`${styles.badge} ${styles.readinessBadge} ${styles.readinessReady}`}>{label}</span>;
  }
  if (state === 'checking') {
    return <span className={`${styles.badge} ${styles.readinessBadge} ${styles.readinessChecking}`}>{label}</span>;
  }
  if (!hasReadinessDetail(readiness)) {
    return <span className={`${styles.badge} ${styles.readinessBadge}`}>{label}</span>;
  }

  // `unknown` keeps the neutral outline deliberately: it is not a verdict
  // about the measure, and must never read as a failure. It only gains the
  // toggle.
  const toneClass = state === 'not_ready' ? `${styles.readinessNotReady} ` : '';
  return (
    <button
      type="button"
      className={`${styles.badge} ${styles.readinessBadge} ${toneClass}${styles.readinessToggle}`}
      aria-expanded={expanded}
      aria-label={`Readiness details for ${measureName}`}
      title={readiness.error || undefined}
      onClick={onToggle}
    >
      {label}
    </button>
  );
}

function ReadinessDetail({ readiness }) {
  const isUnknown = readiness.state === 'unknown';
  return (
    <div className={styles.readinessDetail}>
      {readiness.error && (
        <p className={isUnknown ? styles.readinessNeutralError : styles.readinessError}>{readiness.error}</p>
      )}
      {isUnknown && (
        <p className={styles.readinessNote}>
          This measure was not checked — the check could not complete. Nothing is
          known to be wrong with it. Use “Re-check readiness” to try again.
        </p>
      )}
      {readiness.missing_libraries?.length > 0 && (
        <>
          <h4>Missing libraries</h4>
          <ul>
            {readiness.missing_libraries.map(lib => <li key={lib} className={styles.mono}>{lib}</li>)}
          </ul>
          <p className={styles.readinessNote}>
            The measure server reports only the first library it cannot load, so
            loading this one may reveal another.
          </p>
        </>
      )}
      {readiness.missing_valuesets?.length > 0 && (
        <>
          <h4>Missing value sets ({readiness.missing_valuesets.length})</h4>
          <ul>
            {readiness.missing_valuesets.map(vs => <li key={vs} className={styles.mono}>{vs}</li>)}
          </ul>
        </>
      )}
    </div>
  );
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
  const [rechecking, setRechecking] = useState(false);
  const [refreshFailed, setRefreshFailed] = useState(false);
  const [confirm, setConfirm] = useState(null);
  const [expandedId, setExpandedId] = useState(null);
  const fileInputRef = useRef(null);
  const toast = useToast();
  const { query } = useSearch();
  const { mcs } = useConnection();

  const loadMeasures = useCallback(async ({ quiet = false } = {}) => {
    if (!quiet) setLoading(true);
    if (!quiet) setError(null);
    try {
      const data = await getMeasures();
      setMeasures(Array.isArray(data) ? data : data.measures || data.entry || []);
      setMeasuresMcs(Array.isArray(data) ? null : (data.mcs || null));
      setRefreshFailed(false);
    } catch (err) {
      // A QUIET load is the readiness poll, firing every 5s behind a table the
      // user is reading. One transient blip must not replace that table with a
      // full-page "Cannot reach {mcs}" banner that self-heals on the next tick
      // — that flashes, and destroys the list for a failure the user never
      // asked about. Keep the last good data and say so in one line instead.
      if (quiet) {
        setRefreshFailed(true);
        return;
      }
      // A user-initiated load is different: never render a stale list from a
      // previous connection — the whole point of #396 is that an unreachable
      // MCS shows empty, not old data.
      setMeasures([]);
      setMeasuresMcs(null);
      const { issues, errorDetails } = parseFhirError(err.body);
      setError({ message: err.message || 'Cannot reach measure engine', issues, errorDetails });
    } finally {
      if (!quiet) setLoading(false);
    }
  }, []);

  // Re-fetch whenever the active MCS changes (#396) — otherwise the page
  // keeps showing the previous connection's measures after activating a
  // different one in Settings.
  useEffect(() => { loadMeasures(); }, [loadMeasures, mcs.id]);

  // Poll only while a sweep is actually running. A verdict is cached and
  // event-invalidated, so there is nothing to poll for once every row has
  // settled — an unconditional interval would be steady load for no news.
  const anyChecking = measures.some(m => m.readiness?.state === 'checking');
  useEffect(() => {
    if (!anyChecking) return undefined;
    const timer = setInterval(() => { loadMeasures({ quiet: true }); }, 5000);
    return () => clearInterval(timer);
  }, [anyChecking, loadMeasures]);

  // A row that stops having anything to show must not leave an expanded,
  // empty grey detail panel behind — a 5s poll can turn an expanded not-ready
  // row green underneath the user.
  useEffect(() => {
    if (expandedId === null) return;
    const row = measures.find((m, i) => (m.id || i) === expandedId);
    if (!hasReadinessDetail(row?.readiness)) setExpandedId(null);
  }, [measures, expandedId]);

  const handleUploadClick = () => fileInputRef.current?.click();

  const handleRecheck = async () => {
    setRechecking(true);
    try {
      const result = await refreshMeasureReadiness();
      // The backend answers `skipped` when there is no MCSConfig row to write
      // verdicts against. Nothing was queued, so saying nothing at all would
      // make a no-op look like a successful re-check.
      if (result?.status === 'skipped') {
        toast.error('Readiness check skipped: no active measure server connection to check against.');
      }
      await loadMeasures({ quiet: true });
    } catch (err) {
      toast.error(`Could not start readiness check: ${err.message || 'Request failed'}`);
    } finally {
      setRechecking(false);
    }
  };

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
          {!loading && !error && refreshFailed && (
            <div className={styles.staleNote} role="status">
              Couldn’t refresh just now — showing the last result.
            </div>
          )}
        </div>
        <div className={styles.headerActions}>
          {/* Disabled for the whole sweep, not just the milliseconds the POST
              is in flight. Ten clicks used to mean ten concurrent sweeps
              against a shared measure server — exactly what the backend's
              concurrency cap exists to prevent (it caps one sweep, not ten).
              It also makes the "Checking…" label truthful throughout. */}
          <button
            className={styles.retryBtn}
            onClick={handleRecheck}
            disabled={rechecking || anyChecking}
            aria-busy={rechecking || anyChecking}
          >
            {rechecking || anyChecking ? 'Checking…' : 'Re-check readiness'}
          </button>
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
                <th>ID</th><th className={styles.measureCell}>Measure</th><th>Version</th><th>Status</th><th style={{ width: 120 }}>Readiness</th><th style={{ textAlign: 'right' }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {[1, 2, 3].map(i => (
                <tr key={i}>
                  {[90, 200, 60, 80, 110, 100].map((w, j) => (
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
          {/* Wrapped: passing the click handler directly hands React's event
              object in as the options bag. It works only by accident today
              (`event.quiet` is undefined), and would silently mean "quiet"
              the moment another option is added. */}
          <button className={styles.retryBtn} onClick={() => loadMeasures()}>Retry</button>
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
                <th style={{ width: 120 }}>Readiness</th>
                <th style={{ width: 100, textAlign: 'right' }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {visible.length === 0 ? (
                <tr>
                  <td colSpan={6} className={styles.emptyRow}>
                    {q ? `No measures match "${q}".` : 'No measures loaded. Upload a measure bundle to get started.'}
                  </td>
                </tr>
              ) : (
                visible.map((measure, i) => {
                  const key = measure.id || i;
                  const readiness = measure.readiness;
                  const isExpanded = expandedId === key;
                  return (
                    <React.Fragment key={key}>
                      <tr className={styles.row}>
                        <td data-label="ID"><span className={styles.mono}>{extractCmsId(measure.id) || measure.id || '--'}</span></td>
                        <td data-label="Measure" className={`${styles.measureName} ${styles.measureCell}`}>{getMeasureDisplayName(measure)}</td>
                        <td data-label="Version" className={styles.mono} style={{ color: 'var(--text-muted)' }}>{getMeasureVersion(measure)}</td>
                        <td data-label="Status"><StatusBadge status={getMeasureStatus(measure)} /></td>
                        <td data-label="Readiness">
                          <ReadinessBadge
                            readiness={readiness}
                            expanded={isExpanded}
                            onToggle={() => setExpandedId(isExpanded ? null : key)}
                            measureName={getMeasureDisplayName(measure)}
                          />
                        </td>
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
                      {isExpanded && hasReadinessDetail(readiness) && (
                        <tr className={styles.detailRow}>
                          <td colSpan={6}><ReadinessDetail readiness={readiness} /></td>
                        </tr>
                      )}
                    </React.Fragment>
                  );
                })
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
