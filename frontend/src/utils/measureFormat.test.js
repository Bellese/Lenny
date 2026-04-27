import { extractCmsId, cleanMeasureName, measureOptionLabel } from './measureFormat';

describe('extractCmsId', () => {
  it('returns null for null input', () => expect(extractCmsId(null)).toBeNull());
  it('returns null for undefined input', () => expect(extractCmsId(undefined)).toBeNull());
  it('returns null for empty string', () => expect(extractCmsId('')).toBeNull());
  it('extracts standard CMS ID: CMS122v1 → CMS122', () => expect(extractCmsId('CMS122v1')).toBe('CMS122'));
  it('extracts from CMS###FHIR... pattern: CMS529FHIRDiabetes → CMS529', () =>
    expect(extractCmsId('CMS529FHIRDiabetesAssess')).toBe('CMS529'));
  it('extracts from CMSFHIR###... pattern: CMSFHIR529HybridHW → CMS529', () =>
    expect(extractCmsId('CMSFHIR529HybridHospitalWideReadmission')).toBe('CMS529'));
  it('returns null for non-CMS strings', () => expect(extractCmsId('EXM-529')).toBeNull());
});

describe('cleanMeasureName', () => {
  it('returns empty string for falsy input', () => expect(cleanMeasureName('')).toBe(''));
  it('returns empty string for null', () => expect(cleanMeasureName(null)).toBe(''));
  it('strips trailing " FHIR" suffix', () =>
    expect(cleanMeasureName('Breast Cancer Screening FHIR')).toBe('Breast Cancer Screening'));
  it('strips trailing FHIR case-insensitively', () =>
    expect(cleanMeasureName('Breast Cancer Screening fhir')).toBe('Breast Cancer Screening'));
  it('leaves names without FHIR suffix unchanged', () =>
    expect(cleanMeasureName('Diabetes HbA1c')).toBe('Diabetes HbA1c'));
});

describe('measureOptionLabel', () => {
  it('formats both cmsId and name: CMS71 — Anticoagulation Therapy', () =>
    expect(measureOptionLabel('CMS71v1', 'Anticoagulation Therapy FHIR')).toBe('CMS71 — Anticoagulation Therapy'));
  it('falls back to name only when id has no CMS pattern', () =>
    expect(measureOptionLabel('EXM-529', 'Some Measure')).toBe('Some Measure'));
  it('falls back to cmsId only when rawName is absent', () =>
    expect(measureOptionLabel('CMS529FHIRDiabetes', '')).toBe('CMS529'));
  it('falls back to raw id when neither cmsId nor name is present', () =>
    expect(measureOptionLabel('EXM-529', '')).toBe('EXM-529'));
  it('returns empty string when all args are empty', () =>
    expect(measureOptionLabel('', '')).toBe(''));
});
