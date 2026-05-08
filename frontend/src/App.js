import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Routes, Route, NavLink, Navigate, useLocation, useNavigate } from 'react-router-dom';
import styles from './App.module.css';
import MeasuresPage from './pages/MeasuresPage';
import JobsPage from './pages/JobsPage';
import ResultsPage from './pages/ResultsPage';
import SettingsPage from './pages/SettingsPage';
import ValidationPage from './pages/ValidationPage';
import { getHealth, getAdminSettings } from './api/client';
import {
  MeasuresIcon, JobsIcon, ResultsIcon, ValidateIcon,
  SettingsIcon, SearchIcon, XIcon, SunIcon, MoonIcon, GithubIcon,
} from './components/Icons';
import HealthChipGroup from './components/HealthChipGroup';
import SearchContext from './contexts/SearchContext';
import pkg from '../package.json';

const ALL_NAV_ITEMS = [
  { path: '/measures',   label: 'Measures',   Icon: MeasuresIcon,  kbd: 'M', feature: null },
  { path: '/jobs',       label: 'Jobs',        Icon: JobsIcon,      kbd: 'J', feature: null },
  { path: '/results',    label: 'Results',     Icon: ResultsIcon,   kbd: 'E', feature: null },
  { path: '/validation', label: 'Validation',  Icon: ValidateIcon,  kbd: 'V', feature: 'validation' },
];

const PAGE_TITLE = {
  '/measures': 'Measures',
  '/jobs': 'Jobs',
  '/results': 'Results',
  '/validation': 'Validation',
  '/settings': 'Settings',
};

const SEARCH_PLACEHOLDER = {
  '/measures': 'Search measures…',
  '/jobs': 'Search jobs…',
  '/results': 'Search patients…',
  '/validation': 'Search validation runs…',
  '/settings': 'Search…',
};

const HEALTH_KINDS = [
  { kind: 'cdr', healthKey: 'cdr', settingsHash: '#cdr-connections' },
  { kind: 'mcs', healthKey: 'measure_engine', settingsHash: '#mcs-connections' },
];

// Debounce: only flip to 'unreachable' after this many consecutive failed probes.
const FAILURE_DEBOUNCE = 2;

function MenuIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
      <path d="M3 5h12M3 9h12M3 13h12" />
    </svg>
  );
}

export default function App() {
  const location = useLocation();
  const navigate = useNavigate();
  const [navOpen, setNavOpen] = useState(false);
  // Per-kind chip state: { cdr: {...}, mcs: {...} }. Each entry: { state, name, errorDetails }.
  const [chips, setChips] = useState({
    cdr: { state: 'pending', name: '', errorDetails: null },
    mcs: { state: 'pending', name: '', errorDetails: null },
  });
  const failureCounts = useRef({ cdr: 0, mcs: 0 });
  const [theme, setTheme] = useState(() => {
    const current = localStorage.getItem('lenny-theme');
    if (current) return current;
    const legacy = localStorage.getItem('mct2-theme');
    if (legacy) {
      localStorage.setItem('lenny-theme', legacy);
      localStorage.removeItem('mct2-theme');
      return legacy;
    }
    return 'light';
  });
  const [query, setQuery] = useState('');
  const [features, setFeatures] = useState({ validation: false });
  const searchRef = useRef(null);

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark');
    localStorage.setItem('lenny-theme', theme);
  }, [theme]);

  useEffect(() => {
    setQuery('');
    setNavOpen(false);
  }, [location.pathname]);

  // Multi-kind health probe
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
        next[kind] = { state: nextState, name: '', errorDetails: null };
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
        next[kind] = { state: 'healthy', name: section.name || '', errorDetails: null };
      } else {
        failureCounts.current[kind] = failureCounts.current[kind] + 1;
        const debounced = failureCounts.current[kind] >= FAILURE_DEBOUNCE;
        next[kind] = {
          state: debounced ? 'unreachable' : 'pending',
          name: section.name || '',
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

  useEffect(() => {
    getAdminSettings()
      .then(s => setFeatures({ validation: s.validation_enabled ?? false }))
      .catch(() => {});
    const h = (e) => setFeatures({ validation: e.detail.validation_enabled ?? false });
    window.addEventListener('admin-settings-changed', h);
    return () => window.removeEventListener('admin-settings-changed', h);
  }, []);

  useEffect(() => {
    const h = (e) => {
      const active = document.activeElement;
      const isInput = active.tagName === 'INPUT' || active.tagName === 'SELECT' || active.tagName === 'TEXTAREA' || active.isContentEditable;

      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        searchRef.current?.focus();
        return;
      }
      if (e.key === 'Escape' && document.activeElement === searchRef.current) {
        searchRef.current.blur();
        setQuery('');
        return;
      }
      if (e.key === 'Escape') {
        setNavOpen(false);
        return;
      }
      if (!isInput && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey) {
        if (e.key === 'm' || e.key === 'M') navigate('/measures');
        else if (e.key === 'j' || e.key === 'J') navigate('/jobs');
        else if (e.key === 'e' || e.key === 'E') navigate('/results');
        else if ((e.key === 'v' || e.key === 'V') && features.validation) navigate('/validation');
      }
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [navigate, features]);

  const navItems = ALL_NAV_ITEMS.filter(({ feature }) => !feature || features[feature]);
  const basePath = '/' + location.pathname.split('/')[1];
  const pageTitle = PAGE_TITLE[basePath] || 'Lenny';
  const searchPlaceholder = SEARCH_PLACEHOLDER[basePath] || 'Search…';
  const cdrChip = chips.cdr;
  const cdrOk = cdrChip.state === 'healthy';

  return (
    <SearchContext.Provider value={{ query, setQuery }}>
      <div className={`${styles.screen} ${navOpen ? styles.navOpen : ''}`}>
        <button
          className={styles.navBackdrop}
          type="button"
          aria-label="Close navigation"
          onClick={() => setNavOpen(false)}
        />
        {/* Brand */}
        <div className={styles.brand}>
          <div className={styles.brandMark}>L</div>
          <span className={styles.brandName}>Lenny</span>
        </div>

        {/* Topbar */}
        <header className={styles.topbar}>
          <button
            className={styles.hamburger}
            type="button"
            aria-label="Open navigation"
            aria-expanded={navOpen}
            onClick={() => setNavOpen(true)}
          >
            <MenuIcon />
          </button>
          <span className={styles.crumb}>{pageTitle}</span>
          <div className={styles.spacer} />
          <div className={styles.topbarRight}>
            <HealthChipGroup
              chips={chips}
              kinds={HEALTH_KINDS}
              onChipClick={(hash) => navigate(`/settings${hash}`)}
            />
            <div className={styles.searchWrap}>
              <SearchIcon className={styles.searchIcon} />
              <input
                ref={searchRef}
                className={styles.searchInput}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={searchPlaceholder}
                aria-label="Search"
              />
              {query
                ? <button className={styles.searchClear} onClick={() => setQuery('')} aria-label="Clear search"><XIcon /></button>
                : <kbd className={styles.kbdInline}>⌘K</kbd>
              }
            </div>
            <button
              className={styles.themeBtn}
              onClick={() => setTheme(t => t === 'light' ? 'dark' : 'light')}
              aria-label={`Switch to ${theme === 'light' ? 'dark' : 'light'} mode`}
              title={`Switch to ${theme === 'light' ? 'dark' : 'light'} mode`}
            >
              {theme === 'light' ? <MoonIcon /> : <SunIcon />}
            </button>
          </div>
        </header>

        {/* Sidebar nav */}
        <nav className={styles.nav} aria-label="Main navigation">
          <button
            className={styles.navClose}
            type="button"
            aria-label="Close navigation"
            onClick={() => setNavOpen(false)}
          >
            <XIcon />
            <span>Close</span>
          </button>
          <div className={styles.navGroupLabel}>Workspace</div>
          {navItems.map(({ path, label, Icon, kbd }) => (
            <NavLink
              key={path}
              to={path}
              className={({ isActive }) => `${styles.navItem} ${isActive ? styles.navItemActive : ''}`}
            >
              <Icon className={styles.navIcon} />
              <span className={styles.navLabel}>{label}</span>
              <span className={styles.navKbd}>{kbd}</span>
            </NavLink>
          ))}

          <div className={styles.navGroupLabel} style={{ marginTop: 16 }}>Data source</div>
          <div className={styles.dataSourceItem}>
            <span className={styles.navIcon}>
              <span className={`${styles.smallDot} ${cdrOk ? styles.smallDotOk : styles.smallDotErr}`} />
            </span>
            <span className={styles.navLabel}>{cdrChip.name || 'Local CDR'}</span>
          </div>

          <NavLink
            to="/settings"
            className={({ isActive }) => `${styles.navItem} ${styles.navItemSettings} ${isActive ? styles.navItemActive : ''}`}
          >
            <SettingsIcon className={styles.navIcon} />
            <span className={styles.navLabel}>Settings</span>
          </NavLink>

          <div className={styles.statusFooter}>
            <div
              className={styles.statusRow}
              title={!cdrOk && cdrChip.errorDetails?.hint ? cdrChip.errorDetails.hint : undefined}
            >
              <span className={`${styles.statusDot} ${cdrOk ? styles.statusDotOk : ''}`} />
              {cdrOk ? 'All services healthy' : 'CDR unavailable'}
            </div>
            <div className={styles.statusVersion}>Lenny · v{pkg.version}</div>
            <a
              className={styles.repoLink}
              href="https://github.com/Bellese/Lenny"
              target="_blank"
              rel="noopener noreferrer"
              aria-label="View Lenny source on GitHub (opens in new tab)"
            >
              <GithubIcon className={styles.repoLinkIcon} />
              <span>github.com/Bellese/Lenny</span>
            </a>
          </div>
        </nav>

        {/* Main content */}
        <main className={styles.main} role="main">
          <Routes>
            <Route path="/" element={<Navigate to="/measures" replace />} />
            <Route path="/measures" element={<MeasuresPage />} />
            <Route path="/jobs" element={<JobsPage />} />
            <Route path="/results" element={<ResultsPage />} />
            <Route path="/results/:jobId" element={<ResultsPage />} />
            <Route path="/validation" element={<ValidationPage />} />
            <Route path="/settings" element={<SettingsPage />} />
          </Routes>
        </main>
      </div>
    </SearchContext.Provider>
  );
}
