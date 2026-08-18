import React, { createContext, useContext, useState, useCallback, useRef, useEffect } from 'react';
import { getHealth } from '../api/client';

// Health-payload kind -> response key. This is the health-poll's own concern
// (mapping GET /health sections to internal chip kinds) and is separate from
// any UI-only kind ordering/routing that lives in the consuming components.
const HEALTH_KINDS = [
  { kind: 'cdr', healthKey: 'cdr' },
  { kind: 'mcs', healthKey: 'measure_engine' },
];

// Debounce: only flip to 'unreachable' after this many consecutive failed probes.
const FAILURE_DEBOUNCE = 2;

const DEFAULT_VALUE = {
  cdr: { id: null, name: '', state: 'pending', errorDetails: null },
  mcs: { id: null, name: '', state: 'pending', isReadOnly: false, errorDetails: null },
  refresh: () => {},
};

const ConnectionContext = createContext(DEFAULT_VALUE);

export default ConnectionContext;

export function useConnection() {
  return useContext(ConnectionContext);
}

export function ConnectionProvider({ children }) {
  // Per-kind chip state: { cdr: {...}, mcs: {...} }. Each entry carries
  // everything downstream consumers (HealthChipGroup, MeasuresPage,
  // JobsPage, App's sidebar) need: state, name, id, isReadOnly, errorDetails.
  const [chips, setChips] = useState({
    cdr: { state: 'pending', name: '', id: null, errorDetails: null },
    mcs: { state: 'pending', name: '', id: null, isReadOnly: false, errorDetails: null },
  });
  const failureCounts = useRef({ cdr: 0, mcs: 0 });

  // Multi-kind health probe — lifted from App.js essentially as-is (#396).
  const checkHealth = useCallback(async () => {
    let health;
    try {
      health = await getHealth();
    } catch {
      // Network error — bump failure counts for both kinds.
      const next = {};
      for (const { kind } of HEALTH_KINDS) {
        failureCounts.current[kind] = failureCounts.current[kind] + 1;
        const nextState = failureCounts.current[kind] >= FAILURE_DEBOUNCE ? 'unreachable' : 'pending';
        next[kind] = { state: nextState, name: '', id: null, isReadOnly: false, errorDetails: null };
      }
      setChips(prev => ({ ...prev, ...next }));
      return;
    }

    const next = {};
    for (const { kind, healthKey } of HEALTH_KINDS) {
      const section = health?.[healthKey] || {};
      const ok = section.status === 'connected' || section.status === 'healthy';
      if (ok) {
        failureCounts.current[kind] = 0;
        next[kind] = {
          state: 'healthy',
          name: section.name || '',
          id: section.id ?? null,
          isReadOnly: !!section.is_read_only,
          errorDetails: null,
        };
      } else {
        failureCounts.current[kind] = failureCounts.current[kind] + 1;
        const debounced = failureCounts.current[kind] >= FAILURE_DEBOUNCE;
        next[kind] = {
          state: debounced ? 'unreachable' : 'pending',
          name: section.name || '',
          id: section.id ?? null,
          isReadOnly: !!section.is_read_only,
          errorDetails: section.error_details || null,
        };
      }
    }
    setChips(prev => ({ ...prev, ...next }));
  }, []);

  useEffect(() => {
    let interval = null;
    const start = () => {
      if (interval !== null) return;
      checkHealth();
      interval = setInterval(checkHealth, 30000);
    };
    const stop = () => {
      if (interval === null) return;
      clearInterval(interval);
      interval = null;
    };
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') start();
      else stop();
    };
    if (document.visibilityState === 'visible') start();
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      stop();
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [checkHealth]);

  const value = {
    cdr: chips.cdr,
    mcs: chips.mcs,
    refresh: checkHealth,
  };

  return (
    <ConnectionContext.Provider value={value}>
      {children}
    </ConnectionContext.Provider>
  );
}
