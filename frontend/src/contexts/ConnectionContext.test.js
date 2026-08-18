import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import { ConnectionProvider, useConnection } from './ConnectionContext';
import * as api from '../api/client';

jest.mock('../api/client');

function Probe() {
  const { cdr, mcs } = useConnection();
  return (
    <div>
      <span data-testid="mcs-id">{mcs.id}</span>
      <span data-testid="mcs-name">{mcs.name}</span>
      <span data-testid="mcs-state">{mcs.state}</span>
      <span data-testid="mcs-readonly">{String(mcs.isReadOnly)}</span>
      <span data-testid="cdr-id">{cdr.id}</span>
      <span data-testid="cdr-name">{cdr.name}</span>
    </div>
  );
}

describe('ConnectionContext — provider exposes mcs/cdr identity from health (#396)', () => {
  test('exposes mcs id/name/isReadOnly and cdr id/name from GET /health', async () => {
    api.getHealth = jest.fn().mockResolvedValue({
      cdr: { status: 'healthy', name: 'Local CDR', id: 'cdr-1' },
      measure_engine: {
        status: 'healthy',
        name: 'Alphora Sandbox',
        id: 'mcs-2',
        is_read_only: true,
      },
    });

    render(
      <ConnectionProvider>
        <Probe />
      </ConnectionProvider>,
    );

    await waitFor(() => expect(screen.getByTestId('mcs-id')).toHaveTextContent('mcs-2'));
    expect(screen.getByTestId('mcs-name')).toHaveTextContent('Alphora Sandbox');
    expect(screen.getByTestId('mcs-state')).toHaveTextContent('healthy');
    expect(screen.getByTestId('mcs-readonly')).toHaveTextContent('true');
    expect(screen.getByTestId('cdr-id')).toHaveTextContent('cdr-1');
    expect(screen.getByTestId('cdr-name')).toHaveTextContent('Local CDR');
  });

  test('an unreachable measure_engine section reports isReadOnly false and no id, not a stale one', async () => {
    api.getHealth = jest.fn().mockResolvedValue({
      cdr: { status: 'healthy', name: 'Local CDR', id: 'cdr-1' },
      measure_engine: { status: 'error', name: '', error_details: { hint: 'Connection refused' } },
    });

    render(
      <ConnectionProvider>
        <Probe />
      </ConnectionProvider>,
    );

    await waitFor(() => expect(screen.getByTestId('mcs-name')).toHaveTextContent(''));
    expect(screen.getByTestId('mcs-readonly')).toHaveTextContent('false');
  });
});
