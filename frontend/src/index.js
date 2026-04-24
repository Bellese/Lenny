import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import './index.css';
import App from './App';
import { ToastProvider } from './components/Toast';
// Fonts imported in index.css via @fontsource

console.log('INDEX_JS_EXECUTING');
try {
  const root = ReactDOM.createRoot(document.getElementById('root'));
  root.render(
    <React.StrictMode>
      <BrowserRouter>
        <ToastProvider>
          <App />
        </ToastProvider>
      </BrowserRouter>
    </React.StrictMode>
  );
} catch (err) {
  const pre = document.createElement('pre');
  pre.style.cssText = 'color:red;padding:20px;white-space:pre-wrap';
  pre.textContent = err.stack || err.message;
  document.getElementById('root').appendChild(pre);
  console.error('RENDER ERROR:', err);
}
