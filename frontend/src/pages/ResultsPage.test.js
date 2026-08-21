import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import ResultsPage from './ResultsPage';
import { ToastProvider } from '../components/Toast';
import * as api from '../api/client';

jest.mock('../api/client');

function renderAt(path = '/results/1') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <ToastProvider>
        <Routes>
          <Route path="/results/:jobId" element={<ResultsPage />} />
          <Route path="/results" element={<ResultsPage />} />
        </Routes>
      </ToastProvider>
    </MemoryRouter>,
  );
}

const BASE_JOB = {
  id: 1,
  status: 'completed',
  measure_id: 'CMS165',
  measure_name: 'Controlling High Blood Pressure',
  period_start: '2025-01-01',
  period_end: '2025-12-31',
};

function mockCommonApi() {
  api.getAdminSettings = jest.fn().mockResolvedValue({ comparison_enabled: false, validation_enabled: false });
  api.getJobs = jest.fn().mockResolvedValue([BASE_JOB]);
  api.getJobComparison = jest.fn().mockResolvedValue(null);
}

describe('ResultsPage — error phase labels (#error_phase mapping)', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  test('maps error_phase "submit" to the DEQM submission failure label', async () => {
    mockCommonApi();
    api.getResults = jest.fn().mockResolvedValue({
      job_id: 1,
      total_patients: 1,
      failed_patients: 1,
      populations: { initial_population: 0, denominator: 0, numerator: 0, denominator_exclusion: 0, numerator_exclusion: 0 },
      performance_rate: null,
      patients: [
        {
          id: 101,
          patient_id: 'pt-1',
          patient_name: 'Jane Doe',
          populations: { error: true, error_message: 'HAPI returned 400' },
          status: 'error',
          error_message: 'HAPI returned 400',
          error_phase: 'submit',
          error_details: null,
        },
      ],
    });

    renderAt();

    await waitFor(() => expect(api.getResults).toHaveBeenCalled());
    expect(
      await screen.findByText(/Couldn't submit patient data to the measure server/),
    ).toBeInTheDocument();
  });

  test.each([
    ['gather', "Couldn't fetch patient data"],
    ['gather_partial', 'Some data missing'],
    ['evaluate', 'Calculation failed'],
  ])('still maps pre-existing error_phase "%s" to "%s"', async (phase, label) => {
    mockCommonApi();
    api.getResults = jest.fn().mockResolvedValue({
      job_id: 1,
      total_patients: 1,
      failed_patients: 1,
      populations: { initial_population: 0, denominator: 0, numerator: 0, denominator_exclusion: 0, numerator_exclusion: 0 },
      performance_rate: null,
      patients: [
        {
          id: 202,
          patient_id: 'pt-2',
          patient_name: 'John Roe',
          populations: { error: true, error_message: 'boom' },
          status: 'error',
          error_message: 'boom',
          error_phase: phase,
          error_details: null,
        },
      ],
    });

    renderAt();

    await waitFor(() => expect(api.getResults).toHaveBeenCalled());
    expect(await screen.findByText(new RegExp(label))).toBeInTheDocument();
  });
});
