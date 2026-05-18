import { formatDuration } from '../utils/dateFormat';

describe('formatDuration', () => {
  it('returns em-dash when startStr is null', () => {
    expect(formatDuration(null, '2024-01-01T00:01:30Z')).toBe('—');
  });

  it('returns em-dash when startStr is undefined', () => {
    expect(formatDuration(undefined, '2024-01-01T00:01:30Z')).toBe('—');
  });

  it('sub-hour: 1m 30s yields 1m 30s', () => {
    const start = '2024-01-01T00:00:00Z';
    const end = '2024-01-01T00:01:30Z';
    expect(formatDuration(start, end)).toBe('1m 30s');
  });

  it('exactly 0 seconds yields 0m 00s', () => {
    const start = '2024-01-01T00:00:00Z';
    const end = '2024-01-01T00:00:00Z';
    expect(formatDuration(start, end)).toBe('0m 00s');
  });

  it('exactly 1 hour (3600s) yields 1h 0m', () => {
    const start = '2024-01-01T00:00:00Z';
    const end = '2024-01-01T01:00:00Z';
    expect(formatDuration(start, end)).toBe('1h 0m');
  });

  it('multi-hour: 3661s yields 1h 1m', () => {
    const start = '2024-01-01T00:00:00Z';
    const end = new Date(new Date(start).getTime() + 3661 * 1000).toISOString();
    expect(formatDuration(start, end)).toBe('1h 1m');
  });

  it('clock-skew: end before start clamps to 0m 00s', () => {
    const start = '2024-01-01T01:00:00Z';
    const end = '2024-01-01T00:59:00Z';
    expect(formatDuration(start, end)).toBe('0m 00s');
  });

  it('live tick: null endStr uses nowFn', () => {
    const start = '2024-01-01T00:00:00Z';
    const nowMs = new Date('2024-01-01T00:01:30Z').getTime();
    const nowFn = () => nowMs;
    expect(formatDuration(start, null, nowFn)).toBe('1m 30s');
  });

  it('zero-pads seconds below 10', () => {
    const start = '2024-01-01T00:00:00Z';
    const end = '2024-01-01T00:01:05Z';
    expect(formatDuration(start, end)).toBe('1m 05s');
  });

  it('59 minutes 59 seconds stays sub-hour', () => {
    const start = '2024-01-01T00:00:00Z';
    const end = new Date(new Date(start).getTime() + 3599 * 1000).toISOString();
    expect(formatDuration(start, end)).toBe('59m 59s');
  });
});
