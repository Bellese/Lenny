import React, { useEffect, useState } from 'react';
import { Navigate } from 'react-router-dom';
import { getAdminSettings } from '../api/client';
import styles from './GroupsPage.module.css';

export default function GroupsPage() {
  const [enabled, setEnabled] = useState(null); // null = loading, true/false = known

  useEffect(() => {
    let cancelled = false;
    getAdminSettings()
      .then(s => { if (!cancelled) setEnabled(!!s.groups_enabled); })
      .catch(() => { if (!cancelled) setEnabled(false); });
    return () => { cancelled = true; };
  }, []);

  if (enabled === null) return <div className={styles.page} />;
  if (!enabled) return <Navigate to="/measures" replace />;

  return (
    <div className={styles.page}>
      <h1>Groups</h1>
    </div>
  );
}
