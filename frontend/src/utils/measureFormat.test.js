import { extractCmsId, cleanMeasureName, measureOptionLabel, measureDisplayLabel, findMatchingGroup } from './measureFormat';

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
  it('formats both cmsId and name with brackets', () =>
    expect(measureOptionLabel('CMS71v1', 'Anticoagulation Therapy FHIR')).toBe('[CMS71] Anticoagulation Therapy'));
  it('falls back to name only when id has no CMS pattern', () =>
    expect(measureOptionLabel('EXM-529', 'Some Measure')).toBe('Some Measure'));
  it('falls back to cmsId only when rawName is absent', () =>
    expect(measureOptionLabel('CMS529FHIRDiabetes', '')).toBe('CMS529'));
  it('falls back to raw id when neither cmsId nor name is present', () =>
    expect(measureOptionLabel('EXM-529', '')).toBe('EXM-529'));
  it('returns empty string when all args are empty', () =>
    expect(measureOptionLabel('', '')).toBe(''));
});

describe('measureDisplayLabel', () => {
  it('formats cms+name: [CMS122] Breast Cancer Screening', () =>
    expect(measureDisplayLabel('CMS122v1', 'Breast Cancer Screening FHIR')).toBe('[CMS122] Breast Cancer Screening'));
  it('formats cms-only when name is absent', () =>
    expect(measureDisplayLabel('CMS122v1', '')).toBe('[CMS122]'));
  it('formats cms-only when name is null', () =>
    expect(measureDisplayLabel('CMS122v1', null)).toBe('[CMS122]'));
  it('falls back to cleaned name when no CMS ID in id or name', () =>
    expect(measureDisplayLabel('EXM-529', 'Some Measure')).toBe('Some Measure'));
  it('falls back gracefully when idOrUrl is a full URL (extractCmsId requires ^CMS prefix)', () =>
    expect(measureDisplayLabel('http://example.com/CMS122v1', 'Breast Cancer Screening')).toBe('Breast Cancer Screening'));
  it('extracts CMS ID from rawName when id has no pattern, returns [cms] name', () =>
    expect(measureDisplayLabel('EXM-529', 'CMS71v1')).toBe('[CMS71] CMS71v1'));
  it('returns empty string when both args are absent', () =>
    expect(measureDisplayLabel('', '')).toBe(''));
  it('returns raw idOrUrl as fallback when nothing else matches', () =>
    expect(measureDisplayLabel('EXM-529', '')).toBe('EXM-529'));
});

describe('findMatchingGroup', () => {
  const groups = [
    { id: 5, name: 'CMS122-cohort' },
    { id: 6, name: 'CMS155-cohort' },
  ];

  it('returns null when measureId is empty', () =>
    expect(findMatchingGroup('', groups)).toBeNull());

  it('returns null when groups array is empty', () =>
    expect(findMatchingGroup('CMS122FHIR-something', [])).toBeNull());

  it('returns null when measureId has no CMS pattern', () =>
    expect(findMatchingGroup('EXM-122', groups)).toBeNull());

  it('returns the group whose name shares the CMS number with the measure', () =>
    expect(findMatchingGroup('CMS122FHIRBreastCancer', groups)).toEqual({ id: 5, name: 'CMS122-cohort' }));

  it('returns null when no group CMS number matches the measure', () =>
    expect(findMatchingGroup('CMS529FHIRDiabetes', groups)).toBeNull());

  it('returns the first match when multiple groups share the CMS number', () => {
    const dupes = [{ id: 10, name: 'CMS122-first' }, { id: 11, name: 'CMS122-second' }];
    expect(findMatchingGroup('CMS122v1', dupes)).toEqual({ id: 10, name: 'CMS122-first' });
  });

  it('falls back to matching on group.id when group.name is absent', () =>
    expect(findMatchingGroup('CMS122FHIR', [{ id: 'CMS122', name: undefined }]))
      .toEqual({ id: 'CMS122', name: undefined }));
});
