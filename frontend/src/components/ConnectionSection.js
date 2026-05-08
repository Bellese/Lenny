import React, { useState, useEffect, useCallback } from 'react';
import styles from '../pages/SettingsPage.module.css';
import {
  getConnections,
  deleteConnection,
  activateConnection,
  getMcsConnections,
  deleteMcsConnection,
  activateMcsConnection,
  probeMcsConnection,
} from '../api/client';
import ConnectionModal from './ConnectionModal';
import ConfirmDialog from './ConfirmDialog';
import OperationOutcomeView from './OperationOutcomeView';
import { useToast } from './Toast';

const KIND_API = {
  cdr: {
    title: 'CDR Connections',
    addLabel: 'Add CDR connection',
    urlField: 'cdr_url',
    showReadOnlyBadge: true,
    list: getConnections,
    activate: activateConnection,
    remove: deleteConnection,
    builtinDeleteHint: 'Cannot delete the built-in CDR',
  },
  mcs: {
    title: 'MCS Connections',
    addLabel: 'Add MCS connection',
    urlField: 'mcs_url',
    showReadOnlyBadge: false,
    list: getMcsConnections,
    activate: activateMcsConnection,
    remove: deleteMcsConnection,
    builtinDeleteHint: 'Cannot delete the built-in Measure Engine',
  },
};

const AUTH_LABEL = { none: 'No Auth', basic: 'Basic', bearer: 'Bearer', smart: 'SMART' };

export default function ConnectionSection({ kind, onChange }) {
  const api = KIND_API[kind];
  const toast = useToast();
  const [connections, setConnections] = useState([]);
  const [loading, setLoading] = useState(true);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState(null);
  const [confirmConn, setConfirmConn] = useState(null);
  // probe state — keyed by connection id so the inline panel can render per row.
  const [probeState, setProbeState] = useState({}); // { [id]: { running, result, error } }

  const load = useCallback(async () => {
    try {
      const data = await api.list();
      setConnections(Array.isArray(data) ? data : data.connections || []);
    } catch {
      setConnections([]);
    }
  }, [api]);

  useEffect(() => {
    load().finally(() => setLoading(false));
  }, [load]);

  const handleSaved = async () => {
    const wasEdit = !!editing;
    setModalOpen(false);
    setEditing(null);
    await load();
    onChange?.();
    toast.success(wasEdit ? 'Connection updated' : 'Connection added');
  };

  const handleActivate = async (id) => {
    try {
      await api.activate(id);
      await load();
      onChange?.();
    } catch (err) {
      toast.error(err.message || 'Failed to activate connection');
    }
  };

  const handleProbe = async (id) => {
    setProbeState(prev => ({ ...prev, [id]: { running: true, result: null, error: null } }));
    try {
      const result = await probeMcsConnection(id);
      setProbeState(prev => ({ ...prev, [id]: { running: false, result, error: null } }));
    } catch (err) {
      const parsed = err.body?.parsed;
      setProbeState(prev => ({
        ...prev,
        [id]: {
          running: false,
          result: null,
          error: {
            message: err.message || 'Probe failed',
            issues: parsed?.issues,
            errorDetails: parsed?.errorDetails,
          },
        },
      }));
    }
  };

  const handleDismissProbe = (id) => {
    setProbeState(prev => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
  };

  const handleDeleteConfirmed = async () => {
    const conn = confirmConn;
    setConfirmConn(null);
    try {
      await api.remove(conn.id);
      await load();
      onChange?.();
      toast.success('Connection deleted');
    } catch (err) {
      const diag = err.body?.detail?.issue?.[0]?.diagnostics;
      toast.error(diag || err.message || 'Failed to delete connection');
    }
  };

  return (
    <>
      <div className={styles.card} id={`${kind}-connections`}>
        <div className={styles.cardHeader}>
          <span className={styles.cardTitle}>{api.title}</span>
          <button className={styles.btnPrimary} onClick={() => { setEditing(null); setModalOpen(true); }}>
            {api.addLabel}
          </button>
        </div>
        {loading ? (
          <div className="skeleton" style={{ height: 60, borderRadius: 8 }} />
        ) : connections.length === 0 ? (
          <div className={styles.emptyState}>No connections configured.</div>
        ) : (
          <div className={styles.connList}>
            {connections.map(conn => {
              const probe = probeState[conn.id];
              const showProbeButton = kind === 'mcs' && conn.is_active;
              return (
                <React.Fragment key={conn.id}>
                  <div className={`${styles.connRow} ${conn.is_active ? styles.connRowActive : ''}`}>
                    <span className={`${styles.connDot} ${conn.is_active ? styles.connDotActive : ''}`} />
                    <div className={styles.connInfo}>
                      <div className={styles.connName}>
                        {conn.name}
                        {conn.is_active && <span className={styles.activeTag}>(active)</span>}
                      </div>
                      <div className={styles.connUrl}>{conn[api.urlField]}</div>
                    </div>
                    <div className={styles.connMeta}>
                      <span className={styles.connBadge}>{AUTH_LABEL[conn.auth_type] || conn.auth_type || 'No Auth'}</span>
                      {api.showReadOnlyBadge && conn.is_read_only && (
                        <span className={`${styles.connBadge} ${styles.connBadgeReadOnly}`}>read-only</span>
                      )}
                    </div>
                    <div className={styles.connActions}>
                      <button className={styles.btnLink} onClick={() => { setEditing(conn); setModalOpen(true); }}>Edit</button>
                      <button className={styles.btnLink} onClick={() => handleActivate(conn.id)} disabled={conn.is_active}>Activate</button>
                      {showProbeButton && (
                        <button
                          className={styles.btnLink}
                          onClick={() => handleProbe(conn.id)}
                          disabled={probe?.running}
                          aria-busy={probe?.running}
                          title="Run $data-requirements against the active MCS to confirm Library/ValueSet resolution"
                        >
                          {probe?.running ? 'Verifying…' : 'Verify with sample evaluate'}
                        </button>
                      )}
                      <button
                        className={`${styles.btnLink} ${styles.btnLinkDanger}`}
                        onClick={() => setConfirmConn(conn)}
                        disabled={conn.is_default || conn.is_active}
                        title={conn.is_default ? api.builtinDeleteHint : conn.is_active ? 'Activate a different connection first' : undefined}
                      >
                        Delete
                      </button>
                    </div>
                  </div>
                  {probe && !probe.running && (probe.result || probe.error) && (
                    <div className={`${styles.probePanel} ${probe.error ? styles.probePanelErr : styles.probePanelOk}`} role="status">
                      <div className={styles.probePanelHeader}>
                        <span aria-hidden="true">{probe.error ? '✗' : (probe.result?.status === 'warning' ? '⚠' : '✓')}</span>
                        <span className={styles.probePanelTitle}>
                          {probe.error
                            ? 'Probe failed'
                            : probe.result?.status === 'warning'
                              ? 'Probe warning'
                              : 'Probe succeeded'}
                        </span>
                        <button
                          className={styles.probeDismiss}
                          onClick={() => handleDismissProbe(conn.id)}
                          aria-label="Dismiss probe result"
                        >
                          &#x2715;
                        </button>
                      </div>
                      <div className={styles.probePanelBody}>
                        {probe.result?.status === 'ok' && (
                          <p className={styles.probeMessage}>
                            <strong>{probe.result.measure_name}</strong>
                            {' — '}
                            {probe.result.data_requirement_count} data requirements resolved
                            {' in '}
                            {probe.result.data_requirements_latency_ms}ms
                          </p>
                        )}
                        {probe.result?.status === 'warning' && (
                          <OperationOutcomeView
                            issues={(probe.result.outcome?.issue || []).map(i => ({
                              severity: i.severity,
                              code: i.code,
                              diagnostics: i.diagnostics,
                            }))}
                          />
                        )}
                        {probe.error && (
                          <>
                            {probe.error.message && <p className={styles.probeMessage}>{probe.error.message}</p>}
                            <OperationOutcomeView issues={probe.error.issues} errorDetails={probe.error.errorDetails} />
                          </>
                        )}
                      </div>
                    </div>
                  )}
                </React.Fragment>
              );
            })}
          </div>
        )}
      </div>

      <ConfirmDialog
        open={!!confirmConn}
        title={`Delete "${confirmConn?.name}"?`}
        body="This connection will be permanently removed. Any active sessions using it will lose access."
        confirmLabel="Delete connection"
        tone="destructive"
        onCancel={() => setConfirmConn(null)}
        onConfirm={handleDeleteConfirmed}
      />

      {modalOpen && (
        <ConnectionModal
          kind={kind}
          connection={editing}
          onClose={() => { setModalOpen(false); setEditing(null); }}
          onSaved={handleSaved}
        />
      )}
    </>
  );
}
