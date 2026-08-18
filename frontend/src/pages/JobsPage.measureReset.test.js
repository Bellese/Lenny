import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import JobsPage from './JobsPage';
import ConnectionContext from '../contexts/ConnectionContext';
import { ToastProvider } from '../components/Toast';
import * as api from '../api/client';

// Covers #396: switching the active MCS must not leave a stale measure_id
// selected in the "New calculation" form once the new measure list no
// longer contains it.
jest.mock('../api/client');

function Harness({ mcsId }) {
  return (
    <ToastProvider>
      <ConnectionContext.Provider
        value={{
          cdr: { id: 'cdr-1', name: 'Local CDR', state: 'healthy' },
          mcs: { id: mcsId, name: 'MCS', state: 'healthy', isReadOnly: false },
          refresh: jest.fn(),
        }}
      >
        <MemoryRouter>
          <JobsPage />
        </MemoryRouter>
      </ConnectionContext.Provider>
    </ToastProvider>
  );
}

describe('JobsPage — stale measure_id reset on MCS switch (#396)', () => {
  test('clears a stale measure_id when the measure list no longer contains it', async () => {
    api.getJobs = jest.fn().mockResolvedValue({ jobs: [] });
    api.getGroups = jest.fn().mockResolvedValue({ groups: [] });
    api.getMeasures = jest.fn()
      .mockResolvedValueOnce({ measures: [{ id: 'CMS999' }] })
      .mockResolvedValueOnce({ measures: [{ id: 'CMS111' }] });

    const { rerender } = render(<Harness mcsId="mcs-a" />);

    await waitFor(() => expect(api.getMeasures).toHaveBeenCalledTimes(1));
    await userEvent.click(await screen.findByRole('button', { name: /New calculation/i }));
    const select = await screen.findByLabelText('Measure');
    await waitFor(() => expect(select.value).toBe('CMS999'));

    // Simulate activating a different MCS connection: mcs.id changes, which
    // must re-trigger loadMeasures and, once the new list arrives, reset the
    // now-stale selection.
    rerender(<Harness mcsId="mcs-b" />);

    await waitFor(() => expect(api.getMeasures).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(select.value).toBe('CMS111'));
  });

  test('leaves a still-present selection untouched across an MCS switch', async () => {
    api.getJobs = jest.fn().mockResolvedValue({ jobs: [] });
    api.getGroups = jest.fn().mockResolvedValue({ groups: [] });
    api.getMeasures = jest.fn()
      .mockResolvedValueOnce({ measures: [{ id: 'CMS999' }, { id: 'CMS111' }] })
      .mockResolvedValueOnce({ measures: [{ id: 'CMS111' }] });

    const { rerender } = render(<Harness mcsId="mcs-a" />);
    await waitFor(() => expect(api.getMeasures).toHaveBeenCalledTimes(1));
    await userEvent.click(await screen.findByRole('button', { name: /New calculation/i }));
    const select = await screen.findByLabelText('Measure');

    await userEvent.selectOptions(select, 'CMS111');
    expect(select.value).toBe('CMS111');

    rerender(<Harness mcsId="mcs-b" />);
    await waitFor(() => expect(api.getMeasures).toHaveBeenCalledTimes(2));
    // still CMS111 — it survived in the new list, so it must not be reset.
    expect(select.value).toBe('CMS111');
  });
});
