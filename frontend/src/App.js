import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Routes, Route, NavLink, Navigate, useLocation, useNavigate } from 'react-router-dom';
import styles from './App.module.css';
import MeasuresPage from './pages/MeasuresPage';
import JobsPage from './pages/JobsPage';
import ResultsPage from './pages/ResultsPage';
import SettingsPage from './pages/SettingsPage';
import ValidationPage from './pages/ValidationPage';
import { getHealth } from './api/client';
import {
  MeasuresIcon, JobsIcon, ResultsIcon, ValidateIcon,
  SettingsIcon, SearchIcon, XIcon, SunIcon, MoonIcon,
} from './components/Icons';
import HealthIndicator from './components/HealthIndicator';
import SearchContext from './contexts/SearchContext';

const NAV_ITEMS = [
  { path: '/measures',   label: 'Measures',   Icon: MeasuresIcon,  kbd: 'M' },
  { path: '/jobs',       label: 'Jobs',        Icon: JobsIcon,      kbd: 'J' },
  { path: '/results',    label: 'Results',     Icon: ResultsIcon,   kbd: 'E' },
  { path: '/validation', label: 'Validation',  Icon: ValidateIcon,  kbd: 'V' },
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
  const [cdrStatus, setCdrStatus] = useState('unknown');
  const [cdrName, setCdrName] = useState('');
  const [cdrErrorDetails, setCdrErrorDetails] = useState(null);
  const [theme, setTheme] = useState(() => localStorage.getItem('mct2-theme') || 'light');
  const [query, setQuery] = useState('');
  const searchRef = useRef(null);

  // Apply dark class to html element
  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark');
    localStorage.setItem('mct2-theme', theme);
  }, [theme]);

  // Clear search on navigation
  useEffect(() => {
    setQuery('');
    setNavOpen(false);
  }, [location.pathname]);

  // CDR health check
  const checkHealth = useCallback(async () => {
    try {
      const health = await getHealth();
      setCdrStatus(health?.cdr?.status ?? 'unknown');
      setCdrName(health?.cdr?.name ?? '');
      setCdrErrorDetails(health?.cdr?.error_details ?? null);
    } catch {
      setCdrStatus('unknown');
      setCdrName('');
      setCdrErrorDetails(null);
    }
  }, []);

  useEffect(() => {
    checkHealth();
    const interval = setInterval(checkHealth, 30000);
    return () => clearInterval(interval);
  }, [checkHealth]);

  // Keyboard shortcuts
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
        else if (e.key === 'v' || e.key === 'V') navigate('/validation');
      }
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [navigate]);

  const basePath = '/' + location.pathname.split('/')[1];
  const pageTitle = PAGE_TITLE[basePath] || 'Lenny';
  const searchPlaceholder = SEARCH_PLACEHOLDER[basePath] || 'Search…';
  const cdrOk = cdrStatus === 'connected' || cdrStatus === 'healthy';

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
            <HealthIndicator status={cdrStatus} name={cdrName} errorDetails={cdrErrorDetails} />
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
          {NAV_ITEMS.map(({ path, label, Icon, kbd }) => (
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
            <span className={styles.navLabel}>{cdrName || 'Local CDR'}</span>
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
              title={!cdrOk && cdrErrorDetails?.hint ? cdrErrorDetails.hint : undefined}
            >
              <span className={`${styles.statusDot} ${cdrOk ? styles.statusDotOk : ''}`} />
              {cdrOk ? 'All services healthy' : 'CDR unavailable'}
            </div>
            <div className={styles.statusVersion}>Lenny · v0.0.3</div>
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
