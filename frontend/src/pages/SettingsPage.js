import React, { useState, useEffect, useCallback } from 'react';
import styles from './SettingsPage.module.css';
import { getSettings, updateSettings, testConnection, getHealth } from '../api/client';
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
  const [settings, setSettings] = useState({
    cdr_url: '',
    auth_type: 'none',
    username: '',
    password: '',
    token: '',
  });
  const [health, setHealth] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const toast = useToast();

  const loadSettings = useCallback(async () => {
    setLoading(true);
    try {
      const data = await getSettings();
      setSettings(prev => ({
        ...prev,
        cdr_url: data.cdr_url || data.cdr?.url || '',
        auth_type: data.auth_type || data.cdr?.auth_type || 'none',
        username: data.username || data.cdr?.username || '',
        password: '', // Never pre-fill password
        token: '', // Never pre-fill token
      }));
    } catch {
      // May not have settings yet
    } finally {
      setLoading(false);
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
    loadSettings();
    loadHealth();
  }, [loadSettings, loadHealth]);

  const handleChange = (field) => (e) => {
    setSettings(prev => ({ ...prev, [field]: e.target.value }));
    setTestResult(null);
  };

  const handleTestConnection = async () => {
    setTesting(true);
    setTestResult(null);
    let succeeded = false;
    try {
      const result = await testConnection({
        cdr_url: settings.cdr_url,
        auth_type: settings.auth_type,
        username: settings.auth_type === 'basic' ? settings.username : undefined,
        password: settings.auth_type === 'basic' ? settings.password : undefined,
        token: settings.auth_type === 'bearer' ? settings.token : undefined,
      });
      setTestResult({ success: true, message: result.message || 'Connected successfully', response_time: result.response_time });
      succeeded = true;
    } catch (err) {
      setTestResult({
        success: false,
        message: err.message || 'Connection failed',
        hint: 'Check that the CDR URL is correct and the server is running.',
      });
    } finally {
      setTesting(false);
    }
    if (succeeded) {
      await loadHealth();
    }
  };

  const handleSave = async (e) => {
    e.preventDefault();
    setSaving(true);
    try {
      await updateSettings({
        cdr_url: settings.cdr_url,
        auth_type: settings.auth_type,
        username: settings.auth_type === 'basic' ? settings.username : undefined,
        password: settings.auth_type === 'basic' ? settings.password : undefined,
        token: settings.auth_type === 'bearer' ? settings.token : undefined,
      });
      toast.success('Settings saved');
      loadHealth();
    } catch (err) {
      toast.error(`Failed to save: ${err.message}`);
    } finally {
      setSaving(false);
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
        {/* CDR Connection */}
        <section className={styles.section}>
          <h2 className={styles.sectionTitle}>CDR Connection</h2>
          <form onSubmit={handleSave} className={styles.form}>
            <div className={styles.formGroup}>
              <label htmlFor="cdr-url" className={styles.label}>CDR URL</label>
              <input
                id="cdr-url"
                type="url"
                value={settings.cdr_url}
                onChange={handleChange('cdr_url')}
                placeholder="http://localhost:8080/fhir"
                className={styles.input}
              />
            </div>

            <div className={styles.formGroup}>
              <label htmlFor="auth-type" className={styles.label}>Authentication</label>
              <select
                id="auth-type"
                value={settings.auth_type}
                onChange={handleChange('auth_type')}
                className={styles.select}
              >
                <option value="none">None</option>
                <option value="basic">Basic Auth</option>
                <option value="bearer">Bearer Token</option>
              </select>
            </div>

            {settings.auth_type === 'basic' && (
              <div className={styles.formRow}>
                <div className={styles.formGroup}>
                  <label htmlFor="username" className={styles.label}>Username</label>
                  <input
                    id="username"
                    type="text"
                    value={settings.username}
                    onChange={handleChange('username')}
                    className={styles.input}
                    autoComplete="username"
                  />
                </div>
                <div className={styles.formGroup}>
                  <label htmlFor="password" className={styles.label}>Password</label>
                  <input
                    id="password"
                    type="password"
                    value={settings.password}
                    onChange={handleChange('password')}
                    className={styles.input}
                    autoComplete="current-password"
                  />
                </div>
              </div>
            )}

            {settings.auth_type === 'bearer' && (
              <div className={styles.formGroup}>
                <label htmlFor="token" className={styles.label}>Bearer Token</label>
                <input
                  id="token"
                  type="password"
                  value={settings.token}
                  onChange={handleChange('token')}
                  className={styles.input}
                  autoComplete="off"
                />
              </div>
            )}

            {/* Test connection result */}
            {testResult && (
              <div className={`${styles.testResult} ${testResult.success ? styles.testSuccess : styles.testFailure}`} role="status">
                <span className={styles.testIcon} aria-hidden="true">
                  {testResult.success ? '\u2713' : '\u2717'}
                </span>
                <div>
                  <p className={styles.testMessage}>{testResult.message}</p>
                  {testResult.response_time && (
                    <p className={styles.testDetail}>Response time: {testResult.response_time}ms</p>
                  )}
                  {testResult.hint && (
                    <p className={styles.testDetail}>{testResult.hint}</p>
                  )}
                </div>
              </div>
            )}

            <div className={styles.actions}>
              <button
                type="button"
                className={styles.secondaryBtn}
                onClick={handleTestConnection}
                disabled={testing || !settings.cdr_url}
                aria-busy={testing}
              >
                {testing ? 'Testing...' : 'Test Connection'}
              </button>
              <button
                type="submit"
                className={styles.primaryBtn}
                disabled={saving}
                aria-busy={saving}
              >
                {saving ? 'Saving...' : 'Save'}
              </button>
            </div>
          </form>
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
              detail={health?.measure_engine_url}
            />
            <StatusIndicator
              label="CDR"
              status={health?.cdr?.status}
              detail={health?.cdr_response_time ? `${health.cdr_response_time}ms` : null}
            />
            <StatusIndicator
              label="Database"
              status={health?.database?.status}
            />
          </div>
        </section>
      </div>
    </div>
  );
}
