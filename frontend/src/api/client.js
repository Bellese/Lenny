const BASE_URL = process.env.REACT_APP_API_URL || 'http://localhost:8000';

class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

async function request(path, options = {}) {
  const url = `${BASE_URL}${path}`;
  const config = {
    headers: {
      'Content-Type': 'application/json',
      ...options.headers,
    },
    ...options,
  };

  // Don't set Content-Type for FormData (let browser set multipart boundary)
  if (options.body instanceof FormData) {
    delete config.headers['Content-Type'];
  }

  const response = await fetch(url, config);

  if (!response.ok) {
    let body = null;
    try {
      body = await response.json();
    } catch {
      // Response may not be JSON
    }
    const message = body?.detail || body?.message || `Request failed: ${response.status} ${response.statusText}`;
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
  return request('/health');
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

// Settings
export function getSettings() {
  return request('/settings');
}

export function updateSettings(data) {
  return request('/settings', {
    method: 'PUT',
    body: JSON.stringify(data),
  });
}

export function testConnection(data) {
  return request('/settings/test-connection', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}
