import React, { useState, useEffect, useCallback } from 'react';
import styles from './SettingsPage.module.css';
import { getConnections, deleteConnection, activateConnection, getHealth, getAdminSettings, updateAdminSettings, wipeMeasureEngine } from '../api/client';
import ConnectionModal from '../components/ConnectionModal';
import ConfirmDialog from '../components/ConfirmDialog';
import { useToast } from '../components/Toast';
import OperationOutcomeView from '../components/OperationOutcomeView';

function DotStatus({ ok }) {
  return <span className={`${styles.statusDot} ${ok ? styles.statusDotOk : styles.statusDotErr}`} />;
}

function Toggle({ checked, onChange, disabled }) {
  return (
    <button
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      className={`${styles.toggle} ${checked ? styles.toggleOn : ''}`}
      onClick={() => onChange(!checked)}
    />
  );
}

export default function SettingsPage() {
  const toast = useToast();
  const [tab, setTab] = useState('connections');
  const [connections, setConnections] = useState([]);
  const [health, setHealth] = useState(null);
  const [loading, setLoading] = useState(true);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingConnection, setEditingConnection] = useState(null);
  const [confirmConn, setConfirmConn] = useState(null);

  // Admin state
  const [adminSettings, setAdminSettings] = useState(null);
  const [adminSaving, setAdminSaving] = useState(false);
  const [confirmWipe, setConfirmWipe] = useState(false);
  const [wiping, setWiping] = useState(false);

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

  const loadAdminSettings = useCallback(async () => {
    try {
      const data = await getAdminSettings();
      setAdminSettings(data);
    } catch {
      setAdminSettings({ validation_enabled: true });
    }
  }, []);

  useEffect(() => {
    loadConnections().finally(() => setLoading(false));
    loadHealth();
    loadAdminSettings();
  }, [loadConnections, loadHealth, loadAdminSettings]);

  const handleModalSaved = async () => {
    const wasEdit = !!editingConnection;
    setModalOpen(false);
    setEditingConnection(null);
    await Promise.all([loadConnections(), loadHealth()]);
    toast.success(wasEdit ? 'Connection updated' : 'Connection added');
  };

  const handleActivate = async (id) => {
    try {
      await activateConnection(id);
      await Promise.all([loadConnections(), loadHealth()]);
    } catch (err) {
      toast.error(err.message || 'Failed to activate connection');
    }
  };

  const handleDeleteConfirmed = async () => {
    const conn = confirmConn;
    setConfirmConn(null);
    try {
      await deleteConnection(conn.id);
      await loadConnections();
      toast.success('Connection deleted');
    } catch (err) {
      const diag = err.body?.detail?.issue?.[0]?.diagnostics;
      toast.error(diag || err.message || 'Failed to delete connection');
    }
  };

  const handleToggleValidation = async (enabled) => {
    setAdminSaving(true);
    try {
      const updated = await updateAdminSettings({ validation_enabled: enabled });
      setAdminSettings(updated);
      window.dispatchEvent(new CustomEvent('admin-settings-changed', { detail: updated }));
      toast.success(enabled ? 'Validation enabled' : 'Validation disabled');
    } catch (err) {
      toast.error(err.message || 'Failed to update setting');
    } finally {
      setAdminSaving(false);
    }
  };

  const handleWipeConfirmed = async () => {
    setConfirmWipe(false);
    setWiping(true);
    try {
      const result = await wipeMeasureEngine();
      toast.success(result.message || 'Measure engine wiped');
    } catch (err) {
      const diag = err.body?.detail?.issue?.[0]?.diagnostics;
      toast.error(diag || err.message || 'Wipe failed');
    } finally {
      setWiping(false);
    }
  };

  const TABS = [
    { id: 'connections', label: 'CDR Connections' },
    { id: 'status', label: 'System Status' },
    { id: 'admin', label: 'Admin' },
  ];

  const statusServices = [
    { label: 'Local Backend', ok: !!health, errorDetails: null },
    {
      label: 'Local Measure Engine',
      ok: health?.measure_engine?.status === 'healthy' || health?.measure_engine?.status === 'connected',
      errorDetails: health?.measure_engine?.error_details || null,
    },
    {
      label: 'Local CDR',
      ok: health?.cdr?.status === 'healthy' || health?.cdr?.status === 'connected',
      detail: health?.cdr?.name || null,
      errorDetails: health?.cdr?.error_details || null,
    },
    {
      label: 'Local Database',
      ok: health?.database?.status === 'healthy' || health?.database?.status === 'connected',
      errorDetails: health?.database?.error_details || null,
    },
  ];

  return (
    <div className={styles.page}>
      <div className={styles.pageHeader}>
        <div>
          <div className={styles.eyebrow}>Configuration</div>
          <h1 className={styles.title}>Settings</h1>
        </div>
      </div>

      <div className={styles.layout}>
        {/* Sub-nav */}
        <nav className={styles.subNav}>
          {TABS.map(t => (
            <button
              key={t.id}
              className={`${styles.subNavItem} ${tab === t.id ? styles.subNavItemActive : ''}`}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </nav>

        {/* Content */}
        <div className={styles.content}>
          {loading ? (
            <div className={styles.card}>
              <div className="skeleton" style={{ height: 80, borderRadius: 8 }} />
            </div>
          ) : tab === 'connections' ? (
            <div className={styles.card}>
              <div className={styles.cardHeader}>
                <span className={styles.cardTitle}>CDR Connections</span>
                <button className={styles.btnPrimary} onClick={() => { setEditingConnection(null); setModalOpen(true); }}>
                  Add connection
                </button>
              </div>
              {connections.length === 0 ? (
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
                        <div className={styles.connUrl}>{conn.cdr_url}</div>
                      </div>
                      <div className={styles.connMeta}>
                        <span className={styles.connBadge}>{{ none: 'No Auth', basic: 'Basic', bearer: 'Bearer', smart: 'SMART' }[conn.auth_type] || conn.auth_type || 'No Auth'}</span>
                        {conn.is_read_only && <span className={`${styles.connBadge} ${styles.connBadgeReadOnly}`}>read-only</span>}
                      </div>
                      <div className={styles.connActions}>
                        <button className={styles.btnLink} onClick={() => { setEditingConnection(conn); setModalOpen(true); }}>Edit</button>
                        <button className={styles.btnLink} onClick={() => handleActivate(conn.id)} disabled={conn.is_active}>Activate</button>
                        <button
                          className={`${styles.btnLink} ${styles.btnLinkDanger}`}
                          onClick={() => setConfirmConn(conn)}
                          disabled={conn.is_default || conn.is_active}
                          title={conn.is_default ? 'Cannot delete the built-in CDR' : conn.is_active ? 'Activate a different connection first' : undefined}
                        >
                          Delete
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ) : tab === 'status' ? (
            <div className={styles.card}>
              <div className={styles.cardHeader}>
                <span className={styles.cardTitle}>System Status</span>
                <button className={styles.btnGhost} onClick={loadHealth} aria-label="Refresh status">
                  <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                    <path d="M12 7a5 5 0 11-1.3-3.3" /><path d="M12 2v3h-3" />
                  </svg>
                  Refresh
                </button>
              </div>
              <div className={styles.statusList}>
                {statusServices.map(s => (
                  <div key={s.label} className={styles.statusItem}>
                    <div className={styles.statusRow}>
                      <DotStatus ok={s.ok} />
                      <span className={styles.statusLabel}>{s.label}</span>
                      <span className={`${styles.statusText} ${s.ok ? styles.statusOk : styles.statusErr}`}>
                        {s.ok ? 'Connected' : 'Unavailable'}
                      </span>
                      {s.detail && <span className={styles.statusDetail}>{s.detail}</span>}
                    </div>
                    {!s.ok && s.errorDetails && (
                      <div className={styles.statusErrorDetails}>
                        <OperationOutcomeView errorDetails={s.errorDetails} />
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          ) : (
            /* Admin tab */
            <div className={styles.adminStack}>
              {/* Measure Engine */}
              <div className={styles.card}>
                <div className={styles.cardHeader}>
                  <span className={styles.cardTitle}>Measure Engine</span>
                </div>
                <div className={styles.adminRow}>
                  <div className={styles.adminRowInfo}>
                    <div className={styles.adminRowLabel}>Wipe measure definitions</div>
                    <div className={styles.adminRowDesc}>
                      Removes all Library, Measure, ValueSet, CodeSystem, and ConceptMap resources
                      from the HAPI measure engine. Use this to recover from CQL compilation failures.
                      The engine re-seeds automatically on the next job run.
                    </div>
                  </div>
                  <button
                    className={styles.btnDanger}
                    onClick={() => setConfirmWipe(true)}
                    disabled={wiping}
                  >
                    {wiping ? 'Wiping…' : 'Wipe engine'}
                  </button>
                </div>
              </div>

              {/* Features */}
              <div className={styles.card}>
                <div className={styles.cardHeader}>
                  <span className={styles.cardTitle}>Features</span>
                </div>
                <div className={styles.adminRow}>
                  <div className={styles.adminRowInfo}>
                    <div className={styles.adminRowLabel}>Validation</div>
                    <div className={styles.adminRowDesc}>
                      Enable or disable the bundle validation workflow.
                    </div>
                  </div>
                  <Toggle
                    checked={adminSettings?.validation_enabled ?? true}
                    onChange={handleToggleValidation}
                    disabled={adminSaving}
                  />
                </div>
              </div>
            </div>
          )}
        </div>
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

      <ConfirmDialog
        open={confirmWipe}
        title="Wipe measure engine definitions?"
        body="This removes all Library, Measure, ValueSet, CodeSystem, and ConceptMap resources from the HAPI measure engine. The engine re-seeds automatically on the next job run. This cannot be undone."
        confirmLabel="Wipe engine"
        tone="destructive"
        onCancel={() => setConfirmWipe(false)}
        onConfirm={handleWipeConfirmed}
      />

      {modalOpen && (
        <ConnectionModal
          connection={editingConnection}
          onClose={() => { setModalOpen(false); setEditingConnection(null); }}
          onSaved={handleModalSaved}
        />
      )}
    </div>
  );
}
