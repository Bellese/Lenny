import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
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
  test('subtitle names the MCS from the GET /measures response, not the health-polled context', async () => {
    api.getMeasures = jest.fn().mockResolvedValue({
      measures: [{ id: 'CMS1' }],
      total: 1,
      mcs: { id: 'mcs-1', name: 'Response-Named MCS', url: 'http://response-mcs' },
    });
    // Context intentionally names a DIFFERENT connection to prove the
    // subtitle describes what the on-screen list actually came from,
    // not wherever the health poll currently believes we're connected.
    renderWithMcs({ name: 'Context-Named MCS' });
    expect(await screen.findByText('1 measure on Response-Named MCS')).toBeInTheDocument();
    expect(screen.queryByText(/Context-Named MCS/)).not.toBeInTheDocument();
  });

  test('falls back to the context MCS name when the response has no mcs block, and never renders "undefined"', async () => {
    api.getMeasures = jest.fn().mockResolvedValue({ measures: [{ id: 'CMS1' }], total: 1 });
    renderWithMcs({ name: 'Context-Named MCS' });
    expect(await screen.findByText('1 measure on Context-Named MCS')).toBeInTheDocument();
    expect(screen.queryByText(/undefined/i)).not.toBeInTheDocument();
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

describe('MeasuresPage — readiness (#434)', () => {
  const measureWith = (readiness, overrides = {}) => ({
    id: 'CMS122FHIRDiabetesAssessGreaterThan9Percent',
    name: 'DiabetesAssess',
    title: 'Diabetes: Hemoglobin A1c Poor Control',
    version: '0.5.000',
    status: 'active',
    readiness,
    ...overrides,
  });

  const READY = { state: 'ready', checked_at: '2026-09-10T18:00:00Z', missing_libraries: [], missing_valuesets: [], error: null };
  const NOT_READY = {
    state: 'not_ready',
    checked_at: '2026-09-10T18:00:00Z',
    missing_libraries: ['Status 1.15.000'],
    missing_valuesets: ['http://cts.nlm.nih.gov/fhir/ValueSet/2.16.840.1.113883.3.464.1003.1003'],
    error: 'Could not load source for library Status, version 1.15.000, namespace uri null.',
  };
  const UNKNOWN = { state: 'unknown', checked_at: null, missing_libraries: [], missing_valuesets: [], error: null };
  const CHECKING = { state: 'checking', checked_at: null, missing_libraries: [], missing_valuesets: [], error: null };

  function renderMeasuresPage(measures, mcsOverrides = {}) {
    api.getMeasures = jest.fn().mockResolvedValue({
      measures,
      total: measures.length,
      mcs: { id: 'mcs-1', name: 'Alphora Sandbox' },
    });
    return renderWithMcs(mcsOverrides);
  }

  test('a ready measure shows the ready badge', async () => {
    renderMeasuresPage([measureWith(READY)]);
    expect(await screen.findByText(/^Ready$/)).toBeInTheDocument();
  });

  test('a not-ready measure shows the not-ready badge', async () => {
    renderMeasuresPage([measureWith(NOT_READY)]);
    expect(await screen.findByText(/Not ready/i)).toBeInTheDocument();
  });

  test('an unchecked measure shows Not checked, not a failure', async () => {
    renderMeasuresPage([measureWith(UNKNOWN)]);
    expect(await screen.findByText(/Not checked/i)).toBeInTheDocument();
    expect(screen.queryByText(/Not ready/i)).not.toBeInTheDocument();
  });

  test('a measure being checked shows Checking', async () => {
    renderMeasuresPage([measureWith(CHECKING)]);
    expect(await screen.findByText(/Checking/i)).toBeInTheDocument();
  });

  test('expanding a not-ready measure lists what is missing', async () => {
    renderMeasuresPage([measureWith(NOT_READY)]);
    // The accessible name identifies the measure by its display name (#434
    // Task 7 review Fix 1) — several not-ready rows must not read identically
    // to a screen reader user tabbing through the table.
    const badge = await screen.findByRole('button', {
      name: /Readiness details for Diabetes: Hemoglobin A1c Poor Control/i,
    });
    await userEvent.click(badge);

    expect(await screen.findByText(/Status 1.15.000/)).toBeInTheDocument();
    expect(screen.getByText(/2\.16\.840\.1\.113883\.3\.464\.1003\.1003/)).toBeInTheDocument();
    expect(screen.getByText(/Could not load source for library Status/)).toBeInTheDocument();
  });

  test('the detail names only the first unresolvable library', async () => {
    /* The CQL engine reports one include at a time; the copy must not imply the
       list is exhaustive, or a user fixes one library and is surprised twice. */
    renderMeasuresPage([measureWith(NOT_READY)]);
    await userEvent.click(
      await screen.findByRole('button', {
        name: /Readiness details for Diabetes: Hemoglobin A1c Poor Control/i,
      }),
    );
    expect(screen.getByText(/may reveal another/i)).toBeInTheDocument();
  });

  test('a ready measure exposes no details toggle', async () => {
    renderMeasuresPage([measureWith(READY)]);
    await screen.findByText(/^Ready$/);
    expect(screen.queryByRole('button', { name: /readiness details/i })).not.toBeInTheDocument();
  });

  test('the re-check button posts to the refresh endpoint', async () => {
    api.refreshMeasureReadiness = jest.fn().mockResolvedValue({ status: 'accepted', measures: 1 });
    renderMeasuresPage([measureWith(NOT_READY)]);
    await screen.findByText(/Not ready/i);

    // Wrap the click in act() so the handler's chained promises (refresh,
    // then the quiet reload) resolve inside a tracked act() scope rather
    // than warning after the test moves on.
    await act(async () => {
      await userEvent.click(screen.getByRole('button', { name: /re-check/i }));
    });

    expect(api.refreshMeasureReadiness).toHaveBeenCalled();
  });

  test('the page polls while any measure is still checking', async () => {
    jest.useFakeTimers();
    try {
      renderMeasuresPage([measureWith(CHECKING)]);
      await screen.findByText(/Checking/i);
      const callsAfterLoad = api.getMeasures.mock.calls.length;

      await act(async () => {
        jest.advanceTimersByTime(5000);
      });

      expect(api.getMeasures.mock.calls.length).toBeGreaterThan(callsAfterLoad);
    } finally {
      jest.useRealTimers();
    }
  });

  test('the page never starts polling when nothing is checking from the start', async () => {
    // Distinct from the transition test below: this proves an interval that
    // never began makes no calls, not that a running interval tears down.
    jest.useFakeTimers();
    try {
      renderMeasuresPage([measureWith(READY)]);
      await screen.findByText(/^Ready$/);
      const callsAfterLoad = api.getMeasures.mock.calls.length;

      await act(async () => {
        jest.advanceTimersByTime(15000);
      });

      expect(api.getMeasures.mock.calls.length).toBe(callsAfterLoad);
    } finally {
      jest.useRealTimers();
    }
  });

  test('the page stops polling once a running sweep settles', async () => {
    // The guarantee that actually matters: an interval that IS running (a row
    // started out `checking`) must be torn down once a later poll settles the
    // last checking row — otherwise the page polls forever after the sweep
    // finishes, invisible load with no user-visible signal.
    jest.useFakeTimers();
    try {
      const mcs = { id: 'mcs-1', name: 'Alphora Sandbox' };
      api.getMeasures = jest.fn()
        .mockResolvedValueOnce({ measures: [measureWith(CHECKING)], total: 1, mcs })
        .mockResolvedValue({ measures: [measureWith(READY)], total: 1, mcs });
      renderWithMcs();

      await screen.findByText(/Checking/i);
      expect(api.getMeasures).toHaveBeenCalledTimes(1);

      // One interval tick: the poll's response settles the row to ready.
      await act(async () => {
        jest.advanceTimersByTime(5000);
      });
      await screen.findByText(/^Ready$/);
      expect(api.getMeasures).toHaveBeenCalledTimes(2);

      // Several more intervals' worth of time must produce no further
      // calls — the effect's cleanup must have cleared the timer once
      // anyChecking flipped to false.
      await act(async () => {
        jest.advanceTimersByTime(20000);
      });
      expect(api.getMeasures).toHaveBeenCalledTimes(2);
    } finally {
      jest.useRealTimers();
    }
  });
});
