import React, { useState, useEffect, useCallback } from 'react';
import styles from '../pages/SettingsPage.module.css';
import {
  getConnections,
  deleteConnection,
  activateConnection,
  getMcsConnections,
  deleteMcsConnection,
  activateMcsConnection,
} from '../api/client';
import ConnectionModal from './ConnectionModal';
import ConfirmDialog from './ConfirmDialog';
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
            {connections.map(conn => (
              <div key={conn.id} className={`${styles.connRow} ${conn.is_active ? styles.connRowActive : ''}`}>
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
            ))}
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
