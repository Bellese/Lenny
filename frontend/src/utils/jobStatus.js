export function isActuallyRunning(status) {
  const s = (status || '').toLowerCase();
  return s === 'running' || s === 'in_progress' || s === 'in-progress';
}

export function isRunning(status) {
  return isActuallyRunning(status) || (status || '').toLowerCase() === 'queued' || (status || '').toLowerCase() === 'pending';
}

export function isComplete(status) {
  const s = (status || '').toLowerCase();
  return s === 'completed' || s === 'complete';
}

export function selectActiveJob(jobs) {
  return jobs.find(j => isActuallyRunning(j.status)) ?? jobs.find(j => isRunning(j.status)) ?? null;
}
