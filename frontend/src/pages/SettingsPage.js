import React, { useState, useEffect, useCallback } from 'react';
import styles from './SettingsPage.module.css';
import { getConnections, deleteConnection, activateConnection, getHealth } from '../api/client';
import ConnectionModal from '../components/ConnectionModal';
import { useToast } from '../components/Toast';

function StatusIndicator({ label, status, detail }) {
  const isHealthy = status === 'healthy' || status === 'connected' || status === true;
  const isUnknown = status === 'unknown' || status === undefined || status === null;

  return (
    <div className={styles.statusRow}>
      <span className={styles.statusIcon} aria-hidden="true">
        {isHealthy ? (
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
            <circle cx="8" cy="8" r="7" fill="var(--color-success-light)" stroke="var(--color-success)" strokeWidth="1.5" />
            <path d="M5 8l2 2 4-4" stroke="var(--color-success)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        ) : isUnknown ? (
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
            <circle cx="8" cy="8" r="7" fill="var(--color-bg-secondary)" stroke="var(--color-text-tertiary)" strokeWidth="1.5" />
            <text x="8" y="12" textAnchor="middle" fontSize="10" fill="var(--color-text-tertiary)">?</text>
          </svg>
        ) : (
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
            <circle cx="8" cy="8" r="7" fill="var(--color-error-light)" stroke="var(--color-error)" strokeWidth="1.5" />
            <path d="M5.5 5.5l5 5M10.5 5.5l-5 5" stroke="var(--color-error)" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
        )}
      </span>
      <span className={styles.statusLabel}>{label}</span>
      <span className={`${styles.statusText} ${isHealthy ? styles.healthy : isUnknown ? styles.unknown : styles.unhealthy}`}>
        {isHealthy ? 'Connected' : isUnknown ? 'Unknown' : 'Disconnected'}
      </span>
      {detail && <span className={styles.statusDetail}>{detail}</span>}
    </div>
  );
}

export default function SettingsPage() {
  const toast = useToast();
  const [connections, setConnections] = useState([]);
  const [health, setHealth] = useState(null);
  const [loading, setLoading] = useState(true);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingConnection, setEditingConnection] = useState(null);

  const loadConnections = useCallback(async () => {
    try {
      const data = await getConnections();
      setConnections(Array.isArray(data) ? data : data.connections || []);
    } catch {
      setConnections([]);
    }
  }, []);

  const loadHealth = useCallback(async () => {
    try {
      const data = await getHealth();
      setHealth(data);
    } catch {
      setHealth(null);
    }
  }, []);

  useEffect(() => {
    loadConnections().finally(() => setLoading(false));
    loadHealth();
  }, [loadConnections, loadHealth]);

  const handleOpenAdd = () => {
    setEditingConnection(null);
    setModalOpen(true);
  };

  const handleOpenEdit = (conn) => {
    setEditingConnection(conn);
    setModalOpen(true);
  };

  const handleModalClose = () => {
    setModalOpen(false);
    setEditingConnection(null);
  };

  const handleModalSaved = async () => {
    const wasEdit = !!editingConnection;
    setModalOpen(false);
    setEditingConnection(null);
    await Promise.all([loadConnections(), loadHealth()]);
    toast.success(wasEdit ? 'Connection updated successfully' : 'Connection added successfully');
  };

  const handleActivate = async (id) => {
    try {
      await activateConnection(id);
      await Promise.all([loadConnections(), loadHealth()]);
    } catch (err) {
      alert(err.message || 'Failed to activate connection');
    }
  };

  const handleDelete = async (conn) => {
    if (!window.confirm(`Delete connection "${conn.name}"?`)) return;
    try {
      await deleteConnection(conn.id);
      await loadConnections();
    } catch (err) {
      const diag = err.body?.detail?.issue?.[0]?.diagnostics;
      alert(diag || err.message || 'Failed to delete connection');
    }
  };

  if (loading) {
    return (
      <div className={styles.page} role="status" aria-label="Loading settings">
        <h1 className={styles.title}>Settings</h1>
        <div className={styles.sections}>
          {[1, 2].map(i => (
            <div key={i} className={`skeleton ${styles.skeletonSection}`} />
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className={styles.page}>
      <h1 className={styles.title}>Settings</h1>

      <div className={styles.sections}>
        {/* CDR Connections */}
        <section className={styles.section}>
          <div className={styles.sectionHeader}>
            <h2 className={styles.sectionTitle}>CDR Connections</h2>
            <button className={styles.addBtn} onClick={handleOpenAdd}>
              Add Connection
            </button>
          </div>

          {connections.length === 0 ? (
            <div className={styles.emptyState}>No connections configured.</div>
          ) : (
            <div className={styles.connectionList}>
              {connections.map(conn => (
                <div
                  key={conn.id}
                  className={styles.connectionRow}
                  data-active={conn.is_active ? 'true' : 'false'}
                >
                  <span
                    className={styles.connectionDot}
                    data-active={conn.is_active ? 'true' : 'false'}
                    aria-label={conn.is_active ? 'Active' : 'Inactive'}
                  />
                  <div className={styles.connectionInfo}>
                    <div className={styles.connectionName}>
                      {conn.name}
                      {conn.is_active && <span style={{ marginLeft: 6, fontWeight: 'normal', color: 'var(--color-success)', fontSize: 'var(--font-size-sm)' }}>(active)</span>}
                    </div>
                    <div className={styles.connectionUrl} title={conn.cdr_url}>{conn.cdr_url}</div>
                  </div>
                  <div className={styles.connectionBadges}>
                    <span className={styles.badge}>{{ none: 'No Auth', basic: 'Basic', bearer: 'Bearer', smart: 'SMART' }[conn.auth_type] || conn.auth_type || 'No Auth'}</span>
                    {conn.is_read_only && (
                      <span className={`${styles.badge} ${styles.badgeReadOnly}`}>read-only</span>
                    )}
                  </div>
                  <div className={styles.connectionActions}>
                    <button
                      className={styles.iconBtn}
                      onClick={() => handleOpenEdit(conn)}
                      aria-label={`Edit ${conn.name}`}
                    >
                      Edit
                    </button>
                    <button
                      className={styles.iconBtn}
                      onClick={() => handleActivate(conn.id)}
                      disabled={conn.is_active}
                      aria-label={`Activate ${conn.name}`}
                    >
                      Activate
                    </button>
                    <button
                      className={`${styles.iconBtn} ${styles.iconBtnDanger}`}
                      onClick={() => handleDelete(conn)}
                      disabled={conn.is_default || conn.is_active}
                      title={conn.is_default ? 'Cannot delete the built-in Local CDR' : conn.is_active ? 'Activate a different connection first, then delete this one' : undefined}
                      aria-label={`Delete ${conn.name}`}
                    >
                      Delete
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>

        {/* System Status */}
        <section className={styles.section}>
          <div className={styles.sectionHeader}>
            <h2 className={styles.sectionTitle}>System Status</h2>
            <button className={styles.refreshBtn} onClick={loadHealth} aria-label="Refresh status">
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                <path d="M14 8a6 6 0 11-1.5-4" />
                <path d="M14 2v4h-4" />
              </svg>
            </button>
          </div>
          <div className={styles.statusGrid}>
            <StatusIndicator
              label="Backend"
              status={health ? 'healthy' : 'unknown'}
            />
            <StatusIndicator
              label="Measure Engine"
              status={health?.measure_engine?.status}
              detail={health?.measure_engine?.error}
            />
            <StatusIndicator
              label="CDR"
              status={health?.cdr?.status}
              detail={health?.cdr?.name ? `${health.cdr.name}${health?.cdr?.is_read_only ? ' · read-only' : ''}` : null}
            />
            <StatusIndicator
              label="Database"
              status={health?.database?.status}
            />
          </div>
        </section>
      </div>

      {modalOpen && (
        <ConnectionModal
          connection={editingConnection}
          onClose={handleModalClose}
          onSaved={handleModalSaved}
        />
      )}
    </div>
  );
}
