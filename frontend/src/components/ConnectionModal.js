import React, { useState, useEffect } from 'react';
import styles from './ConnectionModal.module.css';
import { createConnection, updateConnection, testConnection } from '../api/client';

export default function ConnectionModal({ connection, onClose, onSaved }) {
  const isEdit = !!connection;
  const [form, setForm] = useState({
    name: '',
    cdr_url: '',
    auth_type: 'none',
    username: '',
    password: '',
    token: '',
    client_id: '',
    client_secret: '',
    token_endpoint: '',
    is_read_only: false,
  });
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (connection) {
      setForm(prev => ({
        ...prev,
        name: connection.name || '',
        cdr_url: connection.cdr_url || '',
        auth_type: connection.auth_type || 'none',
        is_read_only: connection.is_read_only || false,
      }));
    }
  }, [connection]);

  const handleChange = (field) => (e) => {
    const value = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
    setForm(prev => ({ ...prev, [field]: value }));
    setTestResult(null);
  };

  const buildAuthCredentials = () => {
    if (form.auth_type === 'basic') {
      return form.username || form.password
        ? { username: form.username, password: form.password }
        : null;
    }
    if (form.auth_type === 'bearer') {
      return form.token ? { token: form.token } : null;
    }
    if (form.auth_type === 'smart') {
      return {
        client_id: form.client_id,
        client_secret: form.client_secret,
        token_endpoint: form.token_endpoint,
      };
    }
    return null;
  };

  const handleTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const result = await testConnection({
        cdr_url: form.cdr_url,
        auth_type: form.auth_type,
        auth_credentials: buildAuthCredentials(),
      });
      setTestResult({ success: true, message: result.message || 'Connected successfully', response_time: result.response_time });
    } catch (err) {
      setTestResult({ success: false, message: err.message || 'Connection failed' });
    } finally {
      setTesting(false);
    }
  };

  const handleSave = async (e) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    const payload = {
      name: form.name,
      cdr_url: form.cdr_url,
      auth_type: form.auth_type,
      auth_credentials: buildAuthCredentials(),
      is_read_only: form.is_read_only,
    };
    try {
      if (isEdit) {
        await updateConnection(connection.id, payload);
      } else {
        await createConnection(payload);
      }
      onSaved();
    } catch (err) {
      const diag = err.body?.detail?.issue?.[0]?.diagnostics;
      setError(diag || err.message || 'Failed to save');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className={styles.overlay} onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className={styles.modal} role="dialog" aria-modal="true" aria-label={isEdit ? 'Edit connection' : 'Add connection'}>
        <div className={styles.header}>
          <h2 className={styles.title}>{isEdit ? 'Edit Connection' : 'Add Connection'}</h2>
          <button className={styles.closeBtn} onClick={onClose} aria-label="Close">&#x2715;</button>
        </div>

        <form onSubmit={handleSave} className={styles.form}>
          {error && <div className={styles.errorBanner} role="alert">{error}</div>}

          <div className={styles.formGroup}>
            <label htmlFor="conn-name" className={styles.label}>Name</label>
            <input id="conn-name" type="text" value={form.name} onChange={handleChange('name')} required className={styles.input} placeholder="e.g. Local CDR, Production" />
          </div>

          <div className={styles.formGroup}>
            <label htmlFor="conn-url" className={styles.label}>CDR URL</label>
            <input id="conn-url" type="url" value={form.cdr_url} onChange={handleChange('cdr_url')} required className={styles.input} placeholder="http://localhost:8080/fhir" />
          </div>

          <div className={styles.formGroup}>
            <label htmlFor="conn-auth" className={styles.label}>Authentication</label>
            <select id="conn-auth" value={form.auth_type} onChange={handleChange('auth_type')} className={styles.select}>
              <option value="none">None</option>
              <option value="basic">Basic Auth</option>
              <option value="bearer">Bearer Token</option>
              <option value="smart">SMART on FHIR</option>
            </select>
          </div>

          {form.auth_type === 'basic' && (
            <div className={styles.formRow}>
              <div className={styles.formGroup}>
                <label htmlFor="conn-username" className={styles.label}>Username</label>
                <input id="conn-username" type="text" value={form.username} onChange={handleChange('username')} className={styles.input} autoComplete="username" />
              </div>
              <div className={styles.formGroup}>
                <label htmlFor="conn-password" className={styles.label}>Password</label>
                <input id="conn-password" type="password" value={form.password} onChange={handleChange('password')} className={styles.input} autoComplete="current-password" />
              </div>
            </div>
          )}

          {form.auth_type === 'bearer' && (
            <div className={styles.formGroup}>
              <label htmlFor="conn-token" className={styles.label}>Bearer Token</label>
              <input id="conn-token" type="password" value={form.token} onChange={handleChange('token')} className={styles.input} autoComplete="off" />
            </div>
          )}

          {form.auth_type === 'smart' && (
            <>
              <div className={styles.formGroup}>
                <label htmlFor="conn-client-id" className={styles.label}>Client ID</label>
                <input id="conn-client-id" type="text" value={form.client_id} onChange={handleChange('client_id')} className={styles.input} />
              </div>
              <div className={styles.formGroup}>
                <label htmlFor="conn-client-secret" className={styles.label}>Client Secret</label>
                <input id="conn-client-secret" type="password" value={form.client_secret} onChange={handleChange('client_secret')} className={styles.input} autoComplete="off" />
              </div>
              <div className={styles.formGroup}>
                <label htmlFor="conn-token-endpoint" className={styles.label}>Token Endpoint</label>
                <input id="conn-token-endpoint" type="url" value={form.token_endpoint} onChange={handleChange('token_endpoint')} className={styles.input} placeholder="https://auth.example.com/token" />
              </div>
            </>
          )}

          <div className={styles.formGroup}>
            <label className={styles.checkboxLabel}>
              <input type="checkbox" checked={form.is_read_only} onChange={handleChange('is_read_only')} className={styles.checkbox} />
              Read-only (never write to this CDR)
            </label>
          </div>

          {testResult && (
            <div className={`${styles.testResult} ${testResult.success ? styles.testSuccess : styles.testFailure}`} role="status">
              <span aria-hidden="true">{testResult.success ? '\u2713' : '\u2717'}</span>
              <div className={styles.testContent}>
                <p className={styles.testMessage}>{testResult.message}</p>
                {testResult.response_time && <p className={styles.testDetail}>Response time: {testResult.response_time}ms</p>}
              </div>
              <button className={styles.testDismiss} onClick={() => setTestResult(null)} aria-label="Dismiss">&#x2715;</button>
            </div>
          )}

          <div className={styles.actions}>
            <button type="button" className={styles.secondaryBtn} onClick={handleTest} disabled={testing || !form.cdr_url} aria-busy={testing}>
              {testing ? 'Testing...' : 'Test Connection'}
            </button>
            <div className={styles.actionsSpacer} />
            <button type="button" className={styles.cancelBtn} onClick={onClose}>Cancel</button>
            <button type="submit" className={styles.primaryBtn} disabled={saving} aria-busy={saving}>
              {saving ? 'Saving...' : 'Save'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
