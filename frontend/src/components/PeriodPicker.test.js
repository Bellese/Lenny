import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import PeriodPicker from './PeriodPicker';

// Use a fixed year so tests don't drift
const TEST_YEAR = 2026;

describe('PeriodPicker — year mode (default)', () => {
  it('renders a year select, not date inputs', () => {
    render(<PeriodPicker periodStart="" periodEnd="" onChange={() => {}} defaultYear={TEST_YEAR} />);
    expect(screen.getByRole('combobox', { name: /reporting period/i })).toBeInTheDocument();
    expect(screen.queryByLabelText(/period start/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/period end/i)).not.toBeInTheDocument();
  });

  it('shows the current year (defaultYear) as the selected option', () => {
    render(<PeriodPicker periodStart={`${TEST_YEAR}-01-01`} periodEnd={`${TEST_YEAR}-12-31`} onChange={() => {}} defaultYear={TEST_YEAR} />);
    expect(screen.getByRole('combobox', { name: /reporting period/i })).toHaveValue(String(TEST_YEAR));
  });

  it('shows the past 5 years in the dropdown', () => {
    render(<PeriodPicker periodStart="" periodEnd="" onChange={() => {}} defaultYear={TEST_YEAR} />);
    const select = screen.getByRole('combobox', { name: /reporting period/i });
    const options = Array.from(select.options).map(o => Number(o.value));
    expect(options).toEqual([2022, 2023, 2024, 2025, 2026]);
  });

  it('calls onChange with Jan 1 and Dec 31 when a year is selected', () => {
    const handleChange = jest.fn();
    render(<PeriodPicker periodStart="" periodEnd="" onChange={handleChange} defaultYear={TEST_YEAR} />);
    fireEvent.change(screen.getByRole('combobox', { name: /reporting period/i }), { target: { value: '2025' } });
    expect(handleChange).toHaveBeenCalledWith('2025-01-01', '2025-12-31');
  });

  it('shows a link to enter custom dates', () => {
    render(<PeriodPicker periodStart="" periodEnd="" onChange={() => {}} defaultYear={TEST_YEAR} />);
    expect(screen.getByRole('button', { name: /enter custom dates/i })).toBeInTheDocument();
  });
});

describe('PeriodPicker — toggling to custom mode', () => {
  it('shows date inputs after clicking "Enter custom dates"', () => {
    render(<PeriodPicker periodStart="" periodEnd="" onChange={() => {}} defaultYear={TEST_YEAR} />);
    fireEvent.click(screen.getByRole('button', { name: /enter custom dates/i }));
    expect(screen.getByLabelText(/period start/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/period end/i)).toBeInTheDocument();
  });

  it('hides the year select in custom mode', () => {
    render(<PeriodPicker periodStart="" periodEnd="" onChange={() => {}} defaultYear={TEST_YEAR} />);
    fireEvent.click(screen.getByRole('button', { name: /enter custom dates/i }));
    expect(screen.queryByRole('combobox', { name: /reporting period/i })).not.toBeInTheDocument();
  });

  it('shows a "back to year select" link in custom mode', () => {
    render(<PeriodPicker periodStart="" periodEnd="" onChange={() => {}} defaultYear={TEST_YEAR} />);
    fireEvent.click(screen.getByRole('button', { name: /enter custom dates/i }));
    expect(screen.getByRole('button', { name: /back to year/i })).toBeInTheDocument();
  });

  it('returns to year mode when "Back to year select" is clicked', () => {
    render(<PeriodPicker periodStart="" periodEnd="" onChange={() => {}} defaultYear={TEST_YEAR} />);
    fireEvent.click(screen.getByRole('button', { name: /enter custom dates/i }));
    fireEvent.click(screen.getByRole('button', { name: /back to year/i }));
    expect(screen.getByRole('combobox', { name: /reporting period/i })).toBeInTheDocument();
    expect(screen.queryByLabelText(/period start/i)).not.toBeInTheDocument();
  });

  it('calls onChange with default year dates when returning from custom mode', () => {
    const handleChange = jest.fn();
    render(<PeriodPicker periodStart="2024-03-15" periodEnd="2024-09-30" onChange={handleChange} defaultYear={TEST_YEAR} />);
    fireEvent.click(screen.getByRole('button', { name: /enter custom dates/i }));
    handleChange.mockClear();
    fireEvent.click(screen.getByRole('button', { name: /back to year/i }));
    expect(handleChange).toHaveBeenCalledWith(`${TEST_YEAR}-01-01`, `${TEST_YEAR}-12-31`);
  });
});
