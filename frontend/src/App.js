import React, { useState, useEffect, useCallback } from 'react';
import { Routes, Route, NavLink, Link, Navigate, useLocation } from 'react-router-dom';
import styles from './App.module.css';
import MeasuresPage from './pages/MeasuresPage';
import JobsPage from './pages/JobsPage';
import ResultsPage from './pages/ResultsPage';
import SettingsPage from './pages/SettingsPage';
import ValidationPage from './pages/ValidationPage';
import { getHealth } from './api/client';

function GearIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="10" cy="10" r="3" />
      <path d="M10 1.5a1 1 0 011 1v.74a1 1 0 00.67.95l.12.04a1 1 0 001.06-.2l.52-.53a1 1 0 011.42 0l.7.7a1 1 0 010 1.42l-.52.52a1 1 0 00-.2 1.06l.04.12a1 1 0 00.95.67h.74a1 1 0 011 1v1a1 1 0 01-1 1h-.74a1 1 0 00-.95.67l-.04.12a1 1 0 00.2 1.06l.52.52a1 1 0 010 1.42l-.7.7a1 1 0 01-1.42 0l-.52-.52a1 1 0 00-1.06-.2l-.12.04a1 1 0 00-.67.95v.74a1 1 0 01-1 1H9a1 1 0 01-1-1v-.74a1 1 0 00-.67-.95l-.12-.04a1 1 0 00-1.06.2l-.52.53a1 1 0 01-1.42 0l-.7-.7a1 1 0 010-1.42l.52-.52a1 1 0 00.2-1.06l-.04-.12a1 1 0 00-.95-.67H2.5a1 1 0 01-1-1V9a1 1 0 011-1h.74a1 1 0 00.95-.67l.04-.12a1 1 0 00-.2-1.06l-.53-.52a1 1 0 010-1.42l.7-.7a1 1 0 011.42 0l.52.52a1 1 0 001.06.2l.12-.04a1 1 0 00.67-.95V2.5a1 1 0 011-1z" />
    </svg>
  );
}

function MeasuresIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 3h14v14H3z" />
      <path d="M7 7h6M7 10h6M7 13h4" />
    </svg>
  );
}

function JobsIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="10" cy="10" r="7" />
      <path d="M10 6v4l2.5 2.5" />
    </svg>
  );
}

function ResultsIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 17V9h3v8H3zM8.5 17V5h3v12h-3zM14 17V1h3v16h-3z" />
    </svg>
  );
}

function ValidationIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M10 2l6 3v5c0 4-3 7-6 8-3-1-6-4-6-8V5l6-3z" />
      <path d="M7 10l2 2 4-4" />
    </svg>
  );
}

function HamburgerIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <path d="M3 5h14M3 10h14M3 15h14" />
    </svg>
  );
}

const NAV_ITEMS = [
  { path: '/measures', label: 'Measures', icon: MeasuresIcon },
  { path: '/jobs', label: 'Jobs', icon: JobsIcon },
  { path: '/results', label: 'Results', icon: ResultsIcon },
  { path: '/validation', label: 'Validation', icon: ValidationIcon },
];

export default function App() {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [cdrStatus, setCdrStatus] = useState('unknown');
  const [cdrName, setCdrName] = useState('');
  const [cdrReadOnly, setCdrReadOnly] = useState(false);
  const location = useLocation();

  // Close sidebar on navigation
  useEffect(() => {
    setSidebarOpen(false);
  }, [location.pathname]);

  // Check CDR health on mount
  const checkHealth = useCallback(async () => {
    try {
      const health = await getHealth();
      setCdrStatus(health?.cdr?.status ?? 'unknown');
      setCdrName(health?.cdr?.name ?? '');
      setCdrReadOnly(health?.cdr?.is_read_only ?? false);
    } catch {
      setCdrStatus('unknown');
      setCdrName('');
      setCdrReadOnly(false);
    }
  }, []);

  useEffect(() => {
    checkHealth();
    const interval = setInterval(checkHealth, 30000);
    return () => clearInterval(interval);
  }, [checkHealth]);

  const toggleSidebar = useCallback(() => {
    setSidebarOpen(prev => !prev);
  }, []);

  const closeSidebar = useCallback(() => {
    setSidebarOpen(false);
  }, []);

  return (
    <div className={styles.layout}>
      {/* Top bar */}
      <header className={styles.topbar} role="banner">
        <button
          className={styles.hamburger}
          onClick={toggleSidebar}
          aria-label={sidebarOpen ? 'Close navigation' : 'Open navigation'}
          aria-expanded={sidebarOpen}
        >
          <HamburgerIcon />
        </button>
        <Link to="/measures" className={styles.logo}>MCT2</Link>
        <div className={styles.topbarRight}>
          <div className={styles.cdrIndicator} aria-label={`CDR: ${cdrName || 'Local CDR'}${cdrReadOnly ? ' (read-only)' : ''}, status: ${cdrStatus}`}>
            <span className={styles.cdrDot} data-status={cdrStatus} aria-hidden="true" />
            <span>CDR: {cdrName || 'Local CDR'}{cdrReadOnly ? ' (read-only)' : ''}</span>
          </div>
          <NavLink
            to="/settings"
            className={styles.settingsLink}
            aria-label="Settings"
          >
            <GearIcon />
          </NavLink>
        </div>
      </header>

      <div className={styles.body}>
        {/* Sidebar overlay for tablet */}
        <div
          className={`${styles.overlay} ${sidebarOpen ? styles.overlayVisible : ''}`}
          onClick={closeSidebar}
          aria-hidden="true"
        />

        {/* Sidebar */}
        <nav
          className={`${styles.sidebar} ${sidebarOpen ? styles.sidebarOpen : ''}`}
          role="navigation"
          aria-label="Main navigation"
        >
          <ul className={styles.navList} role="list">
            {NAV_ITEMS.map(({ path, label, icon: Icon }) => (
              <li key={path} className={styles.navItem}>
                <NavLink
                  to={path}
                  className={styles.navLink}
                  aria-current={location.pathname.startsWith(path) ? 'page' : undefined}
                >
                  <span className={styles.navIcon}><Icon /></span>
                  {label}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>

        {/* Main content */}
        <main className={styles.main} role="main" aria-label="Main content">
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

      {/* Bottom tabs (mobile) */}
      <nav className={styles.bottomTabs} role="navigation" aria-label="Mobile navigation">
        <ul className={styles.bottomTabList}>
          {NAV_ITEMS.map(({ path, label, icon: Icon }) => (
            <li key={path}>
              <NavLink
                to={path}
                className={styles.bottomTabLink}
                aria-current={location.pathname.startsWith(path) ? 'page' : undefined}
              >
                <Icon />
                <span>{label}</span>
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
    </div>
  );
}
