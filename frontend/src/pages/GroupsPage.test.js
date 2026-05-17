import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import GroupsPage from './GroupsPage';
import * as api from '../api/client';

jest.mock('../api/client');

function renderAt(path = '/groups') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/groups" element={<GroupsPage />} />
        <Route path="/measures" element={<div data-testid="measures-page">Measures</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('GroupsPage — feature disabled', () => {
  test('redirects to /measures when groups_enabled is false', async () => {
    api.getAdminSettings = jest.fn().mockResolvedValue({ groups_enabled: false });
    renderAt();
    await waitFor(() => expect(screen.getByTestId('measures-page')).toBeInTheDocument());
  });
});

import fs from 'fs';
import path from 'path';

describe('GroupsPage — architecture independence (#322)', () => {
  const FORBIDDEN_IMPORT_FRAGMENTS = [
    '/pages/JobsPage',
    '/pages/MeasuresPage',
    '/pages/ResultsPage',
    '/pages/ValidationPage',
    '/utils/jobStatus',
    '/utils/measureFormat',
  ];

  test('GroupsPage.js does not import measure-pipeline modules', () => {
    const source = fs.readFileSync(
      path.join(__dirname, 'GroupsPage.js'),
      'utf8',
    );
    const offenders = FORBIDDEN_IMPORT_FRAGMENTS.filter(f => source.includes(f));
    expect(offenders).toEqual([]);
  });
});
