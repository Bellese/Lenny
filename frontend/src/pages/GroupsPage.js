import React, { useEffect, useState, useCallback } from 'react';
import { Navigate } from 'react-router-dom';
import { getAdminSettings, getEvaluatableGroups } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';
import styles from './GroupsPage.module.css';

export default function GroupsPage() {
  const [enabled, setEnabled] = useState(null);
  const [groups, setGroups] = useState([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    getAdminSettings()
      .then(s => { if (!cancelled) setEnabled(!!s.groups_enabled); })
      .catch(() => { if (!cancelled) setEnabled(false); });
    return () => { cancelled = true; };
  }, []);

  const loadGroups = useCallback(async () => {
    setLoading(true);
    setListError(null);
    try {
      const data = await getEvaluatableGroups();
      setGroups(data.groups || []);
    } catch (err) {
      setListError(err.message || 'Failed to load groups');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (enabled === true) loadGroups();
  }, [enabled, loadGroups]);

  if (enabled === null) return <div className={styles.page} />;
  if (!enabled) return <Navigate to="/measures" replace />;

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <h1 className={styles.title}>Groups</h1>
        <button
          type="button"
          className={styles.refreshBtn}
          onClick={loadGroups}
          disabled={loading}
        >
          Refresh
        </button>
      </div>
      <p className={styles.subtitle}>
        Showing only Groups with a CQL <code>valueExpression</code>. Other Groups on the CDR are hidden.
      </p>

      {listError && <ErrorBanner message={listError} />}

      {!listError && !loading && groups.length === 0 && (
        <div className={styles.empty}>No CQL-evaluatable Groups found on this CDR.</div>
      )}

      <div className={styles.rows}>
        {groups.map(g => (
          <div key={g.id} className={styles.row} data-testid={`group-row-${g.id}`}>
            <div className={styles.rowMain}>
              <div className={styles.groupHeader}>
                <span className={styles.groupName}>{g.name || g.id}</span>
                <span className={styles.idChip}>{g.id}</span>
                <span className={styles.langChip}>{g.expression_language}</span>
              </div>
              <code className={styles.expression}>{g.expression_preview}</code>
            </div>
            {/* $evaluate button arrives in the next task */}
          </div>
        ))}
      </div>
    </div>
  );
}
