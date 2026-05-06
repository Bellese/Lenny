import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import PatientDetail from './PatientDetail';

jest.mock('../api/client', () => ({
  getEvaluatedResources: jest.fn(() => Promise.resolve({ resources: [] })),
}));

const RESULT_WITH_NESTED_POPULATIONS = {
  id: 3296,
  patient_id: 'c9d33132-11ce-455c-9fda-92c433d8c499',
  patient_name: 'EncounterWithBenzo DENOMPass',
  populations: {
    initial_population: true,
    denominator: true,
    numerator: false,
    denominator_exclusion: false,
    numerator_exclusion: false,
  },
  measure_report: { resourceType: 'MeasureReport' },
};

describe('PatientDetail — Population membership', () => {
  it('renders Yes/No status for each population from result.populations', async () => {
    render(<PatientDetail result={RESULT_WITH_NESTED_POPULATIONS} onClose={() => {}} />);
    const section = await screen.findByText('Population membership');
    const list = section.parentElement;
    expect(list).toHaveTextContent(/Initial population\s*Yes/);
    expect(list).toHaveTextContent(/Denominator\s*Yes/);
    expect(list).toHaveTextContent(/Numerator\s*No/);
  });

  it('renders header badges only for populations the patient is in', async () => {
    render(<PatientDetail result={RESULT_WITH_NESTED_POPULATIONS} onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getAllByText('Initial population').length).toBeGreaterThan(0);
    });
    expect(screen.getAllByText('Initial population').length).toBe(2);
    expect(screen.getAllByText('Denominator').length).toBe(2);
    expect(screen.getAllByText('Numerator').length).toBe(1);
  });

  it('renders nothing in the population list when populations are missing', async () => {
    const minimal = { id: 1, patient_id: 'p1', patient_name: 'X', measure_report: {} };
    render(<PatientDetail result={minimal} onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText('Population membership')).toBeInTheDocument();
    });
    expect(screen.queryByText('Initial population')).not.toBeInTheDocument();
  });
});
