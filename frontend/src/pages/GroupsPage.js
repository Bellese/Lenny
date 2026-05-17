import React, { useEffect, useState, useCallback } from 'react';
import { Navigate } from 'react-router-dom';
import { getAdminSettings, getEvaluatableGroups, evaluateGroup } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';
import OperationOutcomeView from '../components/OperationOutcomeView';
import styles from './GroupsPage.module.css';

function MembersTable({ members }) {
  if (!members || members.length === 0) return null;
  return (
    <table className={styles.membersTable}>
      <thead>
        <tr><th>Name</th><th>ID</th><th>Gender</th><th>Birth date</th></tr>
      </thead>
      <tbody>
        {members.map(m => (
          <tr key={m.id} className={m.lookup_error ? styles.memberError : ''}>
            <td>{m.name ?? <span title={m.lookup_error}>(unavailable)</span>}</td>
            <td><code>{m.id}</code></td>
            <td>{m.gender ?? '--'}</td>
            <td>{m.birth_date ?? '--'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function GroupsPage() {
  const [enabled, setEnabled] = useState(null);
  const [groups, setGroups] = useState([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState(null);
  const [evalState, setEvalState] = useState({});  // {gid: 'running'|'complete'|'error'}
  const [results, setResults] = useState({});      // {gid: {members, evaluated_at, member_count}}
  const [errors, setErrors] = useState({});        // {gid: OperationOutcome | string}

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

  useEffect(() => { if (enabled === true) loadGroups(); }, [enabled, loadGroups]);

  const handleEvaluate = useCallback(async (groupId) => {
    setEvalState(s => ({ ...s, [groupId]: 'running' }));
    setErrors(e => { const c = { ...e }; delete c[groupId]; return c; });
    try {
      const r = await evaluateGroup(groupId);
      setResults(prev => ({ ...prev, [groupId]: r }));
      setEvalState(s => ({ ...s, [groupId]: 'complete' }));
    } catch (err) {
      const oo = err?.body?.detail?.operation_outcome;
      setErrors(e => ({ ...e, [groupId]: oo || err.message || 'Evaluation failed' }));
      setEvalState(s => ({ ...s, [groupId]: 'error' }));
    }
  }, []);

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
        {groups.map(g => {
          const state = evalState[g.id] || 'idle';
          const result = results[g.id];
          const error = errors[g.id];
          const isOpen = state === 'complete' || state === 'error';
          return (
            <div key={g.id} className={styles.row} data-testid={`group-row-${g.id}`}>
              <div className={styles.rowTop}>
                <div className={styles.rowMain}>
                  <div className={styles.groupHeader}>
                    <span className={styles.groupName}>{g.name || g.id}</span>
                    <span className={styles.idChip}>{g.id}</span>
                    <span className={styles.langChip}>{g.expression_language}</span>
                  </div>
                  <code className={styles.expression}>{g.expression_preview}</code>
                </div>
                <button
                  type="button"
                  className={styles.evalBtn}
                  disabled={state === 'running'}
                  onClick={() => handleEvaluate(g.id)}
                >
                  {state === 'running'
                    ? 'Evaluating…'
                    : state === 'complete'
                    ? 'Re-evaluate'
                    : '$evaluate'}
                </button>
              </div>

              {isOpen && (
                <div className={styles.accordion}>
                  {state === 'complete' && result && (
                    <>
                      <div className={styles.accordionHeader}>
                        {result.member_count} {result.member_count === 1 ? 'member' : 'members'}
                        {result.evaluated_at && (
                          <span className={styles.evaluatedAt}> · evaluated {result.evaluated_at}</span>
                        )}
                      </div>
                      <MembersTable members={result.members} />
                    </>
                  )}
                  {state === 'error' && error && (
                    typeof error === 'object'
                      ? <OperationOutcomeView issues={error.issue || []} errorDetails={{ raw_outcome: error }} />
                      : <ErrorBanner message={error} />
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
