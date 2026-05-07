import React, { useState, useEffect } from 'react';
import styles from './ConnectionModal.module.css';
import {
  createConnection,
  updateConnection,
  testConnection,
  createMcsConnection,
  updateMcsConnection,
  testMcsConnection,
} from '../api/client';
import OperationOutcomeView from './OperationOutcomeView';
import ErrorBanner from './ErrorBanner';

const KIND_SPECS = {
  cdr: {
    label: 'CDR',
    titleNoun: 'CDR Connection',
    urlField: 'cdr_url',
    urlLabel: 'CDR URL',
    urlPlaceholder: 'http://localhost:8080/fhir',
    showReadOnly: true,
    api: { create: createConnection, update: updateConnection, test: testConnection },
  },
  mcs: {
    label: 'Measure Engine',
    titleNoun: 'Measure Engine Connection',
    urlField: 'mcs_url',
    urlLabel: 'Measure Engine URL',
    urlPlaceholder: 'http://localhost:8081/fhir',
    showReadOnly: false,
    api: { create: createMcsConnection, update: updateMcsConnection, test: testMcsConnection },
  },
};

export default function ConnectionModal({ kind = 'cdr', connection, onClose, onSaved }) {
  const spec = KIND_SPECS[kind];
  const isEdit = !!connection;
  const [form, setForm] = useState({
    name: '',
    [spec.urlField]: '',
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
        [spec.urlField]: connection[spec.urlField] || '',
        auth_type: connection.auth_type || 'none',
        is_read_only: connection.is_read_only || false,
      }));
    }
  }, [connection, spec.urlField]);

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
      const result = await spec.api.test({
        [spec.urlField]: form[spec.urlField],
        auth_type: form.auth_type,
        auth_credentials: buildAuthCredentials(),
      });
      setTestResult({ success: true, response_time_ms: result.response_time_ms });
    } catch (err) {
      const parsed = err.body?.parsed;
      setTestResult({
        success: false,
        message: err.message || 'Connection failed',
        issues: parsed?.issues,
        errorDetails: parsed?.errorDetails,
      });
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
      [spec.urlField]: form[spec.urlField],
      auth_type: form.auth_type,
      auth_credentials: buildAuthCredentials(),
    };
    if (spec.showReadOnly) {
      payload.is_read_only = form.is_read_only;
    }
    try {
      if (isEdit) {
        await spec.api.update(connection.id, payload);
      } else {
        await spec.api.create(payload);
      }
      onSaved();
    } catch (err) {
      const diag = err.body?.detail?.issue?.[0]?.diagnostics;
      setError(diag || err.message || 'Failed to save');
    } finally {
      setSaving(false);
    }
  };

  const titlePrefix = isEdit ? 'Edit' : 'Add';

  return (
    <div className={styles.overlay} onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className={styles.modal} role="dialog" aria-modal="true" aria-label={`${titlePrefix} ${spec.titleNoun}`}>
        <div className={styles.header}>
          <h2 className={styles.title}>{titlePrefix} {spec.titleNoun}</h2>
          <button className={styles.closeBtn} onClick={onClose} aria-label="Close">&#x2715;</button>
        </div>

        <form onSubmit={handleSave} className={styles.form}>
          {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}

          <div className={styles.formGroup}>
            <label htmlFor="conn-name" className={styles.label}>Name</label>
            <input id="conn-name" type="text" value={form.name} onChange={handleChange('name')} required className={styles.input} placeholder={`e.g. Local ${spec.label}, Production`} />
          </div>

          <div className={styles.formGroup}>
            <label htmlFor="conn-url" className={styles.label}>{spec.urlLabel}</label>
            <input id="conn-url" type="url" value={form[spec.urlField]} onChange={handleChange(spec.urlField)} required className={styles.input} placeholder={spec.urlPlaceholder} />
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

          {spec.showReadOnly && (
            <div className={styles.formGroup}>
              <label className={styles.checkboxLabel}>
                <input type="checkbox" checked={form.is_read_only} onChange={handleChange('is_read_only')} className={styles.checkbox} />
                Read-only (never write to this {spec.label})
              </label>
            </div>
          )}

          {testResult && (
            <div className={`${styles.testResult} ${testResult.success ? styles.testSuccess : styles.testFailure}`} role="status">
              <span aria-hidden="true">{testResult.success ? '✓' : '✗'}</span>
              <div className={styles.testContent}>
                {testResult.success
                  ? <p className={styles.testMessage}>Connected{testResult.response_time_ms != null ? ` in ${testResult.response_time_ms}ms` : ' successfully'}</p>
                  : <>
                      {testResult.message && <p className={styles.testMessage}>{testResult.message}</p>}
                      <OperationOutcomeView issues={testResult.issues} errorDetails={testResult.errorDetails} />
                    </>
                }
              </div>
              <button className={styles.testDismiss} onClick={() => setTestResult(null)} aria-label="Dismiss">&#x2715;</button>
            </div>
          )}

          <div className={styles.actions}>
            <button type="button" className={styles.secondaryBtn} onClick={handleTest} disabled={testing || !form[spec.urlField]} aria-busy={testing}>
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
