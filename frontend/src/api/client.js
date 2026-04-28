import { parseFhirError } from './fhirError';

const BASE_URL = process.env.REACT_APP_API_URL || 'http://localhost:8000';

class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

async function request(path, { _timeout = 20000, ...options } = {}) {
  const url = `${BASE_URL}${path}`;
  const controller = new AbortController();
  const timerId = setTimeout(() => controller.abort(), _timeout);

  const config = {
    headers: {
      'Content-Type': 'application/json',
      ...options.headers,
    },
    ...options,
    signal: controller.signal,
  };

  // Don't set Content-Type for FormData (let browser set multipart boundary)
  if (options.body instanceof FormData) {
    delete config.headers['Content-Type'];
  }

  let response;
  try {
    response = await fetch(url, config);
  } catch (err) {
    clearTimeout(timerId);
    if (err.name === 'AbortError') {
      throw new ApiError(`Request timed out after ${_timeout}ms`, 0, null);
    }
    throw err;
  }
  clearTimeout(timerId);

  if (!response.ok) {
    let body = null;
    try {
      body = await response.json();
    } catch {
      // Response may not be JSON
    }
    const raw = body?.detail || body?.message;
    let message;
    if (!raw) {
      message = `Request failed: ${response.status} ${response.statusText}`;
    } else if (typeof raw === 'string') {
      message = raw;
    } else if (raw?.issue?.length) {
      // FHIR OperationOutcome — use first issue with diagnostics
      const first = raw.issue.find(i => i.diagnostics);
      message = first?.diagnostics || `Request failed: ${response.status} ${response.statusText}`;
    } else if (Array.isArray(raw)) {
      // FastAPI validation error array
      message = raw.map(e => e.msg || JSON.stringify(e)).join('; ');
    } else {
      message = `Request failed: ${response.status} ${response.statusText}`;
    }
    if (body && typeof body === 'object' && !body.parsed) {
      body.parsed = parseFhirError(body);
    }
    throw new ApiError(message, response.status, body);
  }

  // Handle 204 No Content
  if (response.status === 204) {
    return null;
  }

  return response.json();
}

// Health
export function getHealth() {
  return request('/health', { _timeout: 5000 });
}

// Measures
export function getMeasures() {
  return request('/measures');
}

export function uploadMeasure(file) {
  const formData = new FormData();
  formData.append('file', file);
  return request('/measures/upload', {
    method: 'POST',
    body: formData,
  });
}

export function deleteMeasure(id) {
  return request(`/measures/${id}`, {
    method: 'DELETE',
  });
}

// Groups
export function getGroups() {
  return request('/jobs/groups');
}

// Jobs
export function getJobs() {
  return request('/jobs');
}

export function getJob(id) {
  return request(`/jobs/${id}`);
}

export function createJob(data) {
  return request('/jobs', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export function cancelJob(id) {
  return request(`/jobs/${id}/cancel`, {
    method: 'POST',
  });
}

export function deleteJob(id) {
  return request(`/jobs/${id}`, {
    method: 'DELETE',
  });
}

// Results
export function getResults(jobId) {
  const params = jobId ? `?job_id=${jobId}` : '';
  return request(`/results${params}`);
}

export function getResult(id) {
  return request(`/results/${id}`);
}

export function getEvaluatedResources(resultId) {
  return request(`/results/${resultId}/evaluated-resources`);
}

// Connections
export function getConnections() {
  return request('/settings/connections');
}

export function createConnection(data) {
  return request('/settings/connections', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export function getConnection(id) {
  return request(`/settings/connections/${id}`);
}

export function updateConnection(id, data) {
  return request(`/settings/connections/${id}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  });
}

export function deleteConnection(id) {
  return request(`/settings/connections/${id}`, { method: 'DELETE' });
}

export function activateConnection(id) {
  return request(`/settings/connections/${id}/activate`, { method: 'POST' });
}

export function testConnection(data) {
  return request('/settings/test-connection', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

// Validation
export function uploadTestBundle(file) {
  const formData = new FormData();
  formData.append('file', file);
  return request('/validation/upload-bundle', {
    method: 'POST',
    body: formData,
  });
}

export function getUploads() {
  return request('/validation/uploads');
}

export function getExpectedResults() {
  return request('/validation/expected');
}

export function startValidationRun(options = {}) {
  return request('/validation/run', {
    method: 'POST',
    body: JSON.stringify(options),
  });
}

export function getValidationRuns() {
  return request('/validation/runs');
}

export function getValidationRun(runId) {
  return request(`/validation/runs/${runId}`);
}

export function deleteValidationRun(runId) {
  return request(`/validation/runs/${runId}`, {
    method: 'DELETE',
  });
}

// Comparison
export function getJobComparison(jobId) {
  return request(`/jobs/${jobId}/comparison`);
}
