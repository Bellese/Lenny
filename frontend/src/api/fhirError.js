export function parseFhirError(body) {
  const detail = body?.detail;
  if (!detail || typeof detail !== 'object') {
    return { issues: [], errorDetails: null };
  }
  const issues = (detail.issue || []).map(i => ({
    severity: i.severity || 'error',
    code: i.code || 'exception',
    diagnostics: i.diagnostics || null,
  }));
  const errorDetails = detail.error_details || null;
  return { issues, errorDetails };
}
