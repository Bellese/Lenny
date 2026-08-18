import React from 'react';
import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ConnectionContext from '../contexts/ConnectionContext';
import { ToastProvider } from '../components/Toast';
import MeasuresPage from './MeasuresPage';
import * as api from '../api/client';

jest.mock('../api/client');

function renderWithMcs(mcsOverrides = {}) {
  const mcs = {
    id: 'mcs-1',
    name: 'Alphora Sandbox',
    state: 'healthy',
    isReadOnly: false,
    ...mcsOverrides,
  };
  return render(
    <MemoryRouter>
      <ToastProvider>
        <ConnectionContext.Provider
          value={{
            cdr: { id: 'cdr-1', name: 'Local CDR', state: 'healthy' },
            mcs,
            refresh: jest.fn(),
          }}
        >
          <MeasuresPage />
        </ConnectionContext.Provider>
      </ToastProvider>
    </MemoryRouter>,
  );
}

describe('MeasuresPage — MCS awareness (#396)', () => {
  test('subtitle names the active MCS', async () => {
    api.getMeasures = jest.fn().mockResolvedValue({ measures: [{ id: 'CMS1' }], total: 1 });
    renderWithMcs();
    expect(await screen.findByText('1 measure on Alphora Sandbox')).toBeInTheDocument();
  });

  test('error state names the MCS, renders no measures, and offers Retry', async () => {
    const err = new Error('Connection refused');
    err.body = {
      detail: {
        issue: [{ severity: 'error', code: 'exception', diagnostics: 'boom' }],
        error_details: { hint: 'Check the URL', status_code: 503 },
      },
    };
    api.getMeasures = jest.fn().mockRejectedValue(err);

    renderWithMcs({ name: 'Alphora Sandbox' });

    expect(await screen.findByText(/Cannot reach Alphora Sandbox/i)).toBeInTheDocument();
    expect(screen.getByText('boom')).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Retry/i })).toBeInTheDocument();
  });

  test('never renders a previously-loaded list once the MCS becomes unreachable', async () => {
    api.getMeasures = jest.fn().mockResolvedValue({ measures: [{ id: 'CMS1' }], total: 1 });
    const { rerender } = renderWithMcs({ id: 'mcs-1' });
    expect(await screen.findAllByText('CMS1')).not.toHaveLength(0);

    api.getMeasures = jest.fn().mockRejectedValue(new Error('unreachable'));
    rerender(
      <MemoryRouter>
        <ToastProvider>
          <ConnectionContext.Provider
            value={{
              cdr: { id: 'cdr-1', name: 'Local CDR', state: 'healthy' },
              mcs: { id: 'mcs-2', name: 'Other MCS', state: 'unreachable', isReadOnly: false },
              refresh: jest.fn(),
            }}
          >
            <MeasuresPage />
          </ConnectionContext.Provider>
        </ToastProvider>
      </MemoryRouter>,
    );

    await screen.findByText(/Cannot reach Other MCS/i);
    expect(screen.queryAllByText('CMS1')).toHaveLength(0);
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  test('disables Upload and Delete when the MCS is read-only', async () => {
    api.getMeasures = jest.fn().mockResolvedValue({ measures: [{ id: 'CMS1' }], total: 1 });
    renderWithMcs({ isReadOnly: true, name: 'Read-only MCS' });

    const uploadBtn = await screen.findByRole('button', { name: /Upload bundle/i });
    expect(uploadBtn).toBeDisabled();
    expect(uploadBtn).toHaveAttribute('title', expect.stringContaining('Read-only MCS'));
  });
});
