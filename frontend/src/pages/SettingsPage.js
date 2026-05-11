import React, { useState, useEffect, useCallback } from 'react';
import styles from './SettingsPage.module.css';
import { getHealth, getAdminSettings, updateAdminSettings, wipeMeasureEngine } from '../api/client';
import ConnectionSection from '../components/ConnectionSection';
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
  const [health, setHealth] = useState(null);

  // Admin state
  const [adminSettings, setAdminSettings] = useState(null);
  const [adminSaving, setAdminSaving] = useState(false);
  const [confirmWipe, setConfirmWipe] = useState(false);
  const [wiping, setWiping] = useState(false);

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
      setAdminSettings({ validation_enabled: false });
    }
  }, []);

  useEffect(() => {
    loadHealth();
    loadAdminSettings();
  }, [loadHealth, loadAdminSettings]);

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

  const handleToggleComparison = async (enabled) => {
    setAdminSaving(true);
    try {
      const updated = await updateAdminSettings({ comparison_enabled: enabled });
      setAdminSettings(updated);
      window.dispatchEvent(new CustomEvent('admin-settings-changed', { detail: updated }));
      toast.success(enabled ? 'Comparison enabled' : 'Comparison disabled');
    } catch (err) {
      toast.error(err.message || 'Failed to update setting');
    } finally {
      setAdminSaving(false);
    }
  };

  const TABS = [
    { id: 'connections', label: 'Connections' },
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
          {tab === 'connections' ? (
            <div className={styles.adminStack}>
              <ConnectionSection kind="cdr" onChange={loadHealth} />
              <ConnectionSection kind="mcs" onChange={loadHealth} />
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

              {/* Developer Tools */}
              <div className={styles.card}>
                <div className={styles.cardHeader}>
                  <span className={styles.cardTitle}>Developer Tools</span>
                </div>
                <div className={styles.adminRow}>
                  <div className={styles.adminRowInfo}>
                    <div className={styles.adminRowLabel}>Validation</div>
                    <div className={styles.adminRowDesc}>
                      Shows a Validation tab in the sidebar where you can upload FHIR test bundles,
                      trigger measure runs against them, and review per-patient pass/fail results.
                      Enable when testing measure logic against known patient fixtures; hide from
                      end users when the workflow isn&apos;t needed.
                    </div>
                  </div>
                  <Toggle
                    checked={adminSettings?.validation_enabled ?? false}
                    onChange={handleToggleValidation}
                    disabled={adminSaving}
                  />
                </div>
                <div className={styles.adminRow}>
                  <div className={styles.adminRowInfo}>
                    <div className={styles.adminRowLabel}>Comparison</div>
                    <div className={styles.adminRowDesc}>
                      Adds a match/mismatch status column to the Results page, comparing the measure
                      engine&apos;s calculated populations against expected results from an uploaded
                      test bundle. Patients with mismatches are flagged and sorted to the top. Only
                      meaningful when bundles with expected population data have been uploaded via
                      Validation.
                    </div>
                  </div>
                  <Toggle
                    checked={adminSettings?.comparison_enabled ?? false}
                    onChange={handleToggleComparison}
                    disabled={adminSaving}
                  />
                </div>
              </div>
            </div>
          )}
        </div>
      </div>

      <ConfirmDialog
        open={confirmWipe}
        title="Wipe measure engine definitions?"
        body="This removes all Library, Measure, ValueSet, CodeSystem, and ConceptMap resources from the HAPI measure engine. The engine re-seeds automatically on the next job run. This cannot be undone."
        confirmLabel="Wipe engine"
        tone="destructive"
        onCancel={() => setConfirmWipe(false)}
        onConfirm={handleWipeConfirmed}
      />
    </div>
  );
}
