import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import JobsPage from './JobsPage';
import ConnectionContext from '../contexts/ConnectionContext';
import { ToastProvider } from '../components/Toast';
import * as api from '../api/client';

// Covers Task 6: per-job data submission workflow selector + visibility into
// the type-level -> instance-level $submit-data fallback when the active MCS
// doesn't support the type-level operation with bundles.
jest.mock('../api/client');

function Harness() {
  return (
    <ToastProvider>
      <ConnectionContext.Provider
        value={{
          cdr: { id: 'cdr-1', name: 'Local CDR', state: 'healthy' },
          mcs: { id: 'mcs-1', name: 'MCS', state: 'healthy', isReadOnly: false },
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

const BASE_JOB = {
  id: 1,
  measure_id: 'CMS999',
  measure_name: 'Test Measure',
  period_start: '2025-01-01',
  period_end: '2025-12-31',
  cdr_url: 'http://cdr/fhir',
  group_id: null,
  status: 'complete',
  total_patients: 1,
  processed_patients: 1,
  failed_patients: 0,
  total_batches: 1,
  batches_completed: 1,
  delete_requested: false,
  created_at: '2026-08-21T00:00:00Z',
  started_at: '2026-08-21T00:00:01Z',
  completed_at: '2026-08-21T00:01:00Z',
  error_message: null,
};

describe('JobsPage — data submission workflow', () => {
  beforeEach(() => {
    api.getGroups = jest.fn().mockResolvedValue({ groups: [] });
    api.getMeasures = jest.fn().mockResolvedValue({ measures: [{ id: 'CMS999' }] });
    api.createJob = jest.fn().mockResolvedValue({ ...BASE_JOB, workflow: 'deqm_submit_data', submit_data_mode: 'base-fallback' });
  });

  test('modal defaults to direct load and sends the selected workflow', async () => {
    api.getJobs = jest.fn().mockResolvedValue({ jobs: [] });
    render(<Harness />);
    await userEvent.click(await screen.findByRole('button', { name: /New calculation/i }));
    const workflowSelect = await screen.findByLabelText(/Data submission workflow/i);
    expect(workflowSelect.value).toBe('direct_load');
    await userEvent.selectOptions(workflowSelect, 'deqm_submit_data');
    const measureSelect = await screen.findByLabelText('Measure');
    await waitFor(() => expect(measureSelect.value).toBe('CMS999'));
    await userEvent.click(screen.getByRole('button', { name: /Start calculation/i }));
    await waitFor(() =>
      expect(api.createJob).toHaveBeenCalledWith(expect.objectContaining({ workflow: 'deqm_submit_data' }))
    );
  });

  test('creating a DEQM job that falls back to base mode shows a warning toast', async () => {
    // Coverage-audit gap fill: JobsPage.js fires toast.warning() when the
    // creation response comes back with submit_data_mode === 'base-fallback'.
    // No prior test asserted this toast actually renders.
    api.getJobs = jest.fn().mockResolvedValue({ jobs: [] });
    render(<Harness />);
    await userEvent.click(await screen.findByRole('button', { name: /New calculation/i }));
    const workflowSelect = await screen.findByLabelText(/Data submission workflow/i);
    await userEvent.selectOptions(workflowSelect, 'deqm_submit_data');
    const measureSelect = await screen.findByLabelText('Measure');
    await waitFor(() => expect(measureSelect.value).toBe('CMS999'));
    await userEvent.click(screen.getByRole('button', { name: /Start calculation/i }));
    expect(
      await screen.findByText(/MCS does not support type-level \$submit-data with bundles — falling back to instance-level \$submit-data\./i)
    ).toBeInTheDocument();
  });

  test('DEQM job with base-fallback shows the instance-level fallback warning badge', async () => {
    api.getJobs = jest.fn().mockResolvedValue({
      jobs: [{ ...BASE_JOB, workflow: 'deqm_submit_data', submit_data_mode: 'base-fallback' }],
    });
    render(<Harness />);
    const badge = await screen.findByTitle(/does not support type-level \$submit-data/i);
    expect(badge).toHaveTextContent('DEQM');
    // The fallback explanation must survive without a mouse hover — a
    // screen reader needs an accessible name that carries the same
    // message as the title tooltip, not just the visible "DEQM ⚠" text.
    expect(screen.getByLabelText(/does not support type-level \$submit-data/i)).toBe(badge);
  });

  // #414 AC3: the badge must track the mode the job ACTUALLY ran under, not the
  // creation-time capability probe. The backend now persists a runtime
  // downgrade to Job.submit_data_mode (see
  // test_batch_persists_runtime_downgrade_to_job_submit_data_mode); these two
  // assert the UI distinguishes the two modes, so a job that fell back can
  // never render as a clean STU5 job.
  test('a job that ran as STU5 shows the badge with no fallback marker', async () => {
    api.getJobs = jest.fn().mockResolvedValue({
      jobs: [{ ...BASE_JOB, workflow: 'deqm_submit_data', submit_data_mode: 'stu5' }],
    });
    render(<Harness />);
    const badge = await screen.findByTitle('DEQM $submit-data (type-level, bundle)');
    expect(badge).toHaveTextContent('DEQM');
    // The warning marker and the fallback wording belong to base-fallback only.
    expect(badge).not.toHaveTextContent('⚠');
    expect(screen.queryByTitle(/does not support type-level \$submit-data/i)).not.toBeInTheDocument();
  });

  test('the two modes render distinguishably in the same list', async () => {
    // Guards the failure this issue described from the UI side: if the badge
    // ignored submit_data_mode, both rows would look identical and a job that
    // fell back would be indistinguishable from one that did not.
    api.getJobs = jest.fn().mockResolvedValue({
      jobs: [
        { ...BASE_JOB, id: 1, measure_name: 'Ran as STU5', workflow: 'deqm_submit_data', submit_data_mode: 'stu5' },
        { ...BASE_JOB, id: 2, measure_name: 'Fell back', workflow: 'deqm_submit_data', submit_data_mode: 'base-fallback' },
      ],
    });
    render(<Harness />);
    await screen.findByText(/Ran as STU5/);
    const clean = screen.getByTitle('DEQM $submit-data (type-level, bundle)');
    const fellBack = screen.getByTitle(/does not support type-level \$submit-data/i);
    expect(clean).not.toBe(fellBack);
    expect(fellBack).toHaveTextContent('⚠');
    expect(clean).not.toHaveTextContent('⚠');
  });

  test('direct load jobs show no workflow badge', async () => {
    api.getJobs = jest.fn().mockResolvedValue({
      jobs: [{ ...BASE_JOB, workflow: 'direct_load', submit_data_mode: null }],
    });
    render(<Harness />);
    await screen.findByText(/Test Measure/);
    expect(screen.queryByText(/DEQM/)).not.toBeInTheDocument();
  });

  test('the bundles input appears only on the DEQM branch', async () => {
    api.getJobs = jest.fn().mockResolvedValue({ jobs: [] });
    render(<Harness />);
    await userEvent.click(await screen.findByRole('button', { name: /New calculation/i }));
    expect(screen.queryByLabelText(/Bundles per submission/i)).not.toBeInTheDocument();
    await userEvent.selectOptions(await screen.findByLabelText(/Data submission workflow/i), 'deqm_submit_data');
    expect(await screen.findByLabelText(/Bundles per submission/i)).toBeInTheDocument();
  });

  test('it defaults to 1 when no DEQM job exists', async () => {
    api.getJobs = jest.fn().mockResolvedValue({ jobs: [] });
    render(<Harness />);
    await userEvent.click(await screen.findByRole('button', { name: /New calculation/i }));
    await userEvent.selectOptions(await screen.findByLabelText(/Data submission workflow/i), 'deqm_submit_data');
    expect((await screen.findByLabelText(/Bundles per submission/i)).value).toBe('1');
  });

  test('it defaults to the most recent DEQM job\'s REQUESTED value, not its clamped one', async () => {
    // The whole point of storing two columns: a job clamped from 50 to 1 must
    // still offer 50, or one run against a max:1 server would ratchet the
    // operator's preference down permanently.
    api.getJobs = jest.fn().mockResolvedValue({
      jobs: [
        { ...BASE_JOB, id: 2, workflow: 'deqm_submit_data', bundles_per_submission: 1, bundles_per_submission_requested: 50 },
        { ...BASE_JOB, id: 1, workflow: 'deqm_submit_data', bundles_per_submission: 5, bundles_per_submission_requested: 5 },
      ],
    });
    render(<Harness />);
    await userEvent.click(await screen.findByRole('button', { name: /New calculation/i }));
    await userEvent.selectOptions(await screen.findByLabelText(/Data submission workflow/i), 'deqm_submit_data');
    expect((await screen.findByLabelText(/Bundles per submission/i)).value).toBe('50');
  });

  test('direct_load jobs never contribute a default', async () => {
    api.getJobs = jest.fn().mockResolvedValue({
      jobs: [
        { ...BASE_JOB, id: 2, workflow: 'direct_load', bundles_per_submission: null, bundles_per_submission_requested: null },
        { ...BASE_JOB, id: 1, workflow: 'deqm_submit_data', bundles_per_submission: 8, bundles_per_submission_requested: 8 },
      ],
    });
    render(<Harness />);
    await userEvent.click(await screen.findByRole('button', { name: /New calculation/i }));
    await userEvent.selectOptions(await screen.findByLabelText(/Data submission workflow/i), 'deqm_submit_data');
    expect((await screen.findByLabelText(/Bundles per submission/i)).value).toBe('8');
  });

  test('0 is accepted and sent', async () => {
    api.getJobs = jest.fn().mockResolvedValue({ jobs: [] });
    render(<Harness />);
    await userEvent.click(await screen.findByRole('button', { name: /New calculation/i }));
    await userEvent.selectOptions(await screen.findByLabelText(/Data submission workflow/i), 'deqm_submit_data');
    const input = await screen.findByLabelText(/Bundles per submission/i);
    await userEvent.clear(input);
    await userEvent.type(input, '0');
    const measureSelect = await screen.findByLabelText('Measure');
    await waitFor(() => expect(measureSelect.value).toBe('CMS999'));
    await userEvent.click(screen.getByRole('button', { name: /Start calculation/i }));
    await waitFor(() =>
      expect(api.createJob).toHaveBeenCalledWith(expect.objectContaining({ bundles_per_submission: 0 }))
    );
  });

  test('direct_load sends no bundles value at all', async () => {
    api.getJobs = jest.fn().mockResolvedValue({ jobs: [] });
    render(<Harness />);
    await userEvent.click(await screen.findByRole('button', { name: /New calculation/i }));
    const measureSelect = await screen.findByLabelText('Measure');
    await waitFor(() => expect(measureSelect.value).toBe('CMS999'));
    await userEvent.click(screen.getByRole('button', { name: /Start calculation/i }));
    const sent = api.createJob.mock.calls[0][0];
    expect(sent.bundles_per_submission).toBeUndefined();
  });

  test('the helper text states the rule without hardcoding the batch size', async () => {
    api.getJobs = jest.fn().mockResolvedValue({ jobs: [] });
    render(<Harness />);
    await userEvent.click(await screen.findByRole('button', { name: /New calculation/i }));
    await userEvent.selectOptions(await screen.findByLabelText(/Data submission workflow/i), 'deqm_submit_data');
    const help = await screen.findByText(/0 submits every subject in a processing batch/i);
    expect(help).toBeInTheDocument();
    expect(help.textContent).not.toMatch(/\b100\b/);
  });

  test('a clamped creation reports the value the server actually accepted', async () => {
    api.getJobs = jest.fn().mockResolvedValue({ jobs: [] });
    api.createJob = jest.fn().mockResolvedValue({
      ...BASE_JOB, workflow: 'deqm_submit_data', submit_data_mode: 'stu5',
      bundles_per_submission: 1, bundles_per_submission_requested: 50,
    });
    render(<Harness />);
    await userEvent.click(await screen.findByRole('button', { name: /New calculation/i }));
    await userEvent.selectOptions(await screen.findByLabelText(/Data submission workflow/i), 'deqm_submit_data');
    const input = await screen.findByLabelText(/Bundles per submission/i);
    await userEvent.clear(input);
    await userEvent.type(input, '50');
    const measureSelect = await screen.findByLabelText('Measure');
    await waitFor(() => expect(measureSelect.value).toBe('CMS999'));
    await userEvent.click(screen.getByRole('button', { name: /Start calculation/i }));
    expect(await screen.findByText(/reduced .* to 1/i)).toBeInTheDocument();
  });
});
