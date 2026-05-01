export function extractCmsId(str) {
  if (!str) return null;
  // Handles CMS122..., CMS529FHIR..., CMSFHIR529..., etc.
  const match = str.match(/^CMS[A-Za-z]*(\d+)/i);
  return match ? `CMS${match[1]}` : null;
}

export function cleanMeasureName(name) {
  if (!name) return '';
  return name.replace(/\s*FHIR\s*$/i, '').trim();
}

export function findMatchingGroup(measureId, groups) {
  if (!measureId || !groups || !groups.length) return null;
  const cmsId = extractCmsId(measureId);
  if (!cmsId) return null;
  return groups.find(g => extractCmsId(g.name || g.id) === cmsId) ?? null;
}

export function measureOptionLabel(id, rawName) {
  const cmsId = extractCmsId(id);
  const name = cleanMeasureName(rawName || '');
  if (cmsId && name) return `${cmsId} — ${name}`;
  return name || cmsId || id || '';
}
