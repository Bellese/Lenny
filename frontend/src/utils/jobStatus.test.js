import { isActuallyRunning, isRunning, isComplete, selectActiveJob } from './jobStatus';

describe('isActuallyRunning', () => {
  it('returns true for running', () => expect(isActuallyRunning('running')).toBe(true));
  it('returns true for in_progress', () => expect(isActuallyRunning('in_progress')).toBe(true));
  it('returns true for in-progress', () => expect(isActuallyRunning('in-progress')).toBe(true));
  it('returns false for queued', () => expect(isActuallyRunning('queued')).toBe(false));
  it('returns false for pending', () => expect(isActuallyRunning('pending')).toBe(false));
  it('returns false for completed', () => expect(isActuallyRunning('completed')).toBe(false));
  it('returns false for null', () => expect(isActuallyRunning(null)).toBe(false));
  it('returns false for undefined', () => expect(isActuallyRunning(undefined)).toBe(false));
  it('is case-insensitive', () => expect(isActuallyRunning('RUNNING')).toBe(true));
});

describe('isRunning', () => {
  it('returns true for running', () => expect(isRunning('running')).toBe(true));
  it('returns true for queued', () => expect(isRunning('queued')).toBe(true));
  it('returns true for pending', () => expect(isRunning('pending')).toBe(true));
  it('returns false for completed', () => expect(isRunning('completed')).toBe(false));
  it('returns false for failed', () => expect(isRunning('failed')).toBe(false));
  it('returns false for null', () => expect(isRunning(null)).toBe(false));
});

describe('isComplete', () => {
  it('returns true for completed', () => expect(isComplete('completed')).toBe(true));
  it('returns true for complete', () => expect(isComplete('complete')).toBe(true));
  it('returns false for running', () => expect(isComplete('running')).toBe(false));
  it('returns false for null', () => expect(isComplete(null)).toBe(false));
});

describe('selectActiveJob', () => {
  it('prefers running job over queued when both present', () => {
    const jobs = [
      { id: '39', status: 'queued' },
      { id: '38', status: 'queued' },
      { id: '36', status: 'running' },
    ];
    expect(selectActiveJob(jobs).id).toBe('36');
  });

  it('falls back to first queued job when nothing is running', () => {
    const jobs = [
      { id: '39', status: 'queued' },
      { id: '38', status: 'queued' },
    ];
    expect(selectActiveJob(jobs).id).toBe('39');
  });

  it('returns null when no active jobs', () => {
    const jobs = [
      { id: '35', status: 'completed' },
      { id: '34', status: 'failed' },
    ];
    expect(selectActiveJob(jobs)).toBeNull();
  });

  it('returns null for empty array', () => {
    expect(selectActiveJob([])).toBeNull();
  });

  it('picks the first running job when multiple are running', () => {
    const jobs = [
      { id: '37', status: 'running' },
      { id: '36', status: 'running' },
    ];
    expect(selectActiveJob(jobs).id).toBe('37');
  });
});
