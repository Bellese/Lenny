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
  const UNKNOWN_WITH_ERROR = {
    state: 'unknown',
    checked_at: null,
    missing_libraries: [],
    missing_valuesets: [],
    error: 'HTTP 401: the measure server refused the request. Check this connection\u2019s credentials.',
  };
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
    // `{ selector: 'span' }` picks the row's badge specifically: the Re-check
    // BUTTON also reads "Checking…" for the duration of a sweep now (it is
    // disabled while one runs), so a bare /Checking/ matches two nodes.
    expect(await screen.findByText(/^Checking…$/, { selector: 'span' })).toBeInTheDocument();
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
      await screen.findByText(/^Checking…$/, { selector: 'span' });
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

      await screen.findByText(/^Checking…$/, { selector: 'span' });
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
  test('an unknown verdict WITH an error explains itself and points at Re-check', async () => {
    // Three routes reach `unknown` — timeout, 401/403, restart reclaim — and
    // all three record why. Rendering a bare "Not checked" with no toggle and
    // no title threw that away, and because a row now exists the backend never
    // re-claims it: one bad credential and every measure read "Not checked"
    // forever, with no explanation and no hint that Re-check is the cure.
    renderMeasuresPage([measureWith(UNKNOWN_WITH_ERROR)]);

    const badge = await screen.findByRole('button', {
      name: /Readiness details for Diabetes: Hemoglobin A1c Poor Control/i,
    });
    expect(badge).toHaveAttribute('title', expect.stringContaining('refused the request'));
    // Still NOT a failure badge: `unknown` must never read as not-ready.
    expect(badge.className).not.toMatch(/badgeBad/);

    await userEvent.click(badge);
    expect(await screen.findByText(/refused the request/i)).toBeInTheDocument();
    expect(screen.getByText(/Re-check readiness.{0,3} to try again/i)).toBeInTheDocument();
  });

  test('an unknown verdict with nothing to say stays a plain badge with no toggle', async () => {
    renderMeasuresPage([measureWith(UNKNOWN)]);
    await screen.findByText(/Not checked/i);
    expect(screen.queryByRole('button', { name: /readiness details/i })).not.toBeInTheDocument();
  });

  test('a failed background poll keeps the table instead of blanking it', async () => {
    // Before this fix a quiet poll called setError, and the render swapped the
    // whole table for a full-page "Cannot reach {mcs}" banner — then self-healed
    // on the next 5s tick, so the list the user was reading flashed away and back.
    jest.useFakeTimers();
    try {
      const mcs = { id: 'mcs-1', name: 'Alphora Sandbox' };
      api.getMeasures = jest.fn()
        .mockResolvedValueOnce({ measures: [measureWith(CHECKING)], total: 1, mcs })
        .mockRejectedValue(new Error('Connection reset by peer'));
      renderWithMcs();

      await screen.findByText(/^Checking…$/, { selector: 'span' });
      const rows = screen.getAllByText(/Diabetes: Hemoglobin A1c Poor Control/i).length;

      await act(async () => {
        jest.advanceTimersByTime(5000);
      });

      expect(api.getMeasures).toHaveBeenCalledTimes(2);
      expect(screen.queryByText(/Cannot reach/i)).not.toBeInTheDocument();
      expect(screen.getAllByText(/Diabetes: Hemoglobin A1c Poor Control/i)).toHaveLength(rows);
      expect(screen.getByText(/showing the last result/i)).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  test('a user-initiated load still surfaces the failure as a banner', async () => {
    // The counterpart to the test above: quiet-only suppression must not make
    // a real, user-visible failure silent.
    api.getMeasures = jest.fn().mockRejectedValue(new Error('Connection refused'));
    renderWithMcs({ name: 'Alphora Sandbox' });
    expect(await screen.findByText(/Cannot reach Alphora Sandbox/i)).toBeInTheDocument();
  });

  test('Re-check is disabled for the whole sweep, not just the POST', async () => {
    // Ten clicks used to mean ten concurrent sweeps against a shared measure
    // server, defeating the backend's concurrency cap (which bounds one sweep).
    api.refreshMeasureReadiness = jest.fn().mockResolvedValue({ status: 'accepted', measures: 1 });
    renderMeasuresPage([measureWith(CHECKING)]);
    await screen.findByText(/^Checking…$/, { selector: 'span' });

    const button = screen.getByRole('button', { name: /checking/i });
    expect(button).toBeDisabled();
    await userEvent.click(button);
    expect(api.refreshMeasureReadiness).not.toHaveBeenCalled();
  });

  test('Re-check is enabled again once nothing is checking', async () => {
    // Guards the test above against passing for the trivial reason that the
    // button is always disabled.
    renderMeasuresPage([measureWith(READY)]);
    await screen.findByText(/^Ready$/);
    expect(screen.getByRole('button', { name: /re-check readiness/i })).toBeEnabled();
  });

  test('a re-check the backend skipped is reported, not silently ignored', async () => {
    api.refreshMeasureReadiness = jest.fn().mockResolvedValue({ status: 'skipped', measures: 0 });
    renderMeasuresPage([measureWith(NOT_READY)]);
    await screen.findByText(/Not ready/i);

    await act(async () => {
      await userEvent.click(screen.getByRole('button', { name: /re-check readiness/i }));
    });

    expect(await screen.findByText(/skipped/i)).toBeInTheDocument();
  });

  test('an expanded row collapses when it stops having anything to show', async () => {
    // A poll that turns an expanded not-ready row green used to leave an empty
    // grey detail panel behind.
    const mcs = { id: 'mcs-1', name: 'Alphora Sandbox' };
    api.refreshMeasureReadiness = jest.fn().mockResolvedValue({ status: 'accepted', measures: 1 });
    api.getMeasures = jest.fn()
      .mockResolvedValueOnce({ measures: [measureWith(NOT_READY)], total: 1, mcs })
      .mockResolvedValue({ measures: [measureWith(READY)], total: 1, mcs });
    const { container } = renderWithMcs();

    await userEvent.click(
      await screen.findByRole('button', {
        name: /Readiness details for Diabetes: Hemoglobin A1c Poor Control/i,
      }),
    );
    expect(await screen.findByText(/Could not load source for library Status/)).toBeInTheDocument();
    expect(container.querySelectorAll('.detailRow')).toHaveLength(1);

    // Any quiet reload will do; Re-check is the one the user can press.
    await act(async () => {
      await userEvent.click(screen.getByRole('button', { name: /re-check readiness/i }));
    });

    await screen.findByText(/^Ready$/);
    expect(screen.queryByText(/Could not load source for library Status/)).not.toBeInTheDocument();
    // Asserting on the absent TEXT alone would pass with the bug present: a
    // ready verdict has no error and no missing lists, so the leftover panel
    // is empty, not absent. The row itself has to be gone.
    expect(container.querySelectorAll('.detailRow')).toHaveLength(0);
  });
});
