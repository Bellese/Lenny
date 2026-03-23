import React, { createContext, useContext, useState, useCallback, useRef, useEffect } from 'react';
import styles from './Toast.module.css';

const ToastContext = createContext(null);

let toastId = 0;

export function useToast() {
  const context = useContext(ToastContext);
  if (!context) {
    throw new Error('useToast must be used within a ToastProvider');
  }
  return context;
}

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);
  const timersRef = useRef({});

  const removeToast = useCallback((id) => {
    setToasts(prev => prev.filter(t => t.id !== id));
    if (timersRef.current[id]) {
      clearTimeout(timersRef.current[id]);
      delete timersRef.current[id];
    }
  }, []);

  const addToast = useCallback((message, variant = 'success', duration = 5000) => {
    const id = ++toastId;
    setToasts(prev => [...prev, { id, message, variant }]);
    if (duration > 0) {
      timersRef.current[id] = setTimeout(() => {
        removeToast(id);
      }, duration);
    }
    return id;
  }, [removeToast]);

  const success = useCallback((message) => addToast(message, 'success'), [addToast]);
  const error = useCallback((message) => addToast(message, 'error', 8000), [addToast]);
  const warning = useCallback((message) => addToast(message, 'warning', 6000), [addToast]);

  // Cleanup timers on unmount
  useEffect(() => {
    const timers = timersRef.current;
    return () => {
      Object.values(timers).forEach(clearTimeout);
    };
  }, []);

  return (
    <ToastContext.Provider value={{ addToast, success, error, warning, removeToast }}>
      {children}
      <div
        className={styles.container}
        role="status"
        aria-live="polite"
        aria-label="Notifications"
      >
        {toasts.map(toast => (
          <div
            key={toast.id}
            className={`${styles.toast} ${styles[toast.variant]}`}
            role="alert"
          >
            <span className={styles.icon} aria-hidden="true">
              {toast.variant === 'success' && '\u2713'}
              {toast.variant === 'error' && '\u2717'}
              {toast.variant === 'warning' && '\u26A0'}
            </span>
            <span className={styles.message}>{toast.message}</span>
            <button
              className={styles.dismiss}
              onClick={() => removeToast(toast.id)}
              aria-label="Dismiss notification"
            >
              \u00D7
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
