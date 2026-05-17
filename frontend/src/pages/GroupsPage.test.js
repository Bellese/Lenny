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

describe('GroupsPage — list', () => {
  test('renders rows from getEvaluatableGroups', async () => {
    api.getAdminSettings = jest.fn().mockResolvedValue({ groups_enabled: true });
    api.getEvaluatableGroups = jest.fn().mockResolvedValue({
      groups: [
        {
          id: 'g1',
          name: 'Active Adults',
          type: 'person',
          expression_language: 'text/cql-expression',
          expression_preview: 'Patient.active and Patient.age >= 18',
        },
      ],
    });
    renderAt();
    expect(await screen.findByText('Active Adults')).toBeInTheDocument();
    expect(screen.getByText(/Patient\.active and Patient\.age >= 18/)).toBeInTheDocument();
    expect(screen.getByText(/text\/cql-expression/)).toBeInTheDocument();
  });

  test('renders empty state when no groups returned', async () => {
    api.getAdminSettings = jest.fn().mockResolvedValue({ groups_enabled: true });
    api.getEvaluatableGroups = jest.fn().mockResolvedValue({ groups: [] });
    renderAt();
    expect(await screen.findByText(/No CQL-evaluatable Groups/i)).toBeInTheDocument();
  });

  test('renders error banner when list call fails', async () => {
    api.getAdminSettings = jest.fn().mockResolvedValue({ groups_enabled: true });
    api.getEvaluatableGroups = jest.fn().mockRejectedValue(new Error('CDR unreachable'));
    renderAt();
    expect(await screen.findByText(/CDR unreachable/i)).toBeInTheDocument();
  });
});

import userEvent from '@testing-library/user-event';

describe('GroupsPage — $evaluate', () => {
  const enableAndOneGroup = () => {
    api.getAdminSettings = jest.fn().mockResolvedValue({ groups_enabled: true });
    api.getEvaluatableGroups = jest.fn().mockResolvedValue({
      groups: [{
        id: 'g1', name: 'g1', type: 'person',
        expression_language: 'text/cql-expression',
        expression_preview: 'Patient.active',
      }],
    });
  };

  test('clicking $evaluate expands the row with members', async () => {
    enableAndOneGroup();
    api.evaluateGroup = jest.fn().mockResolvedValue({
      group_id: 'g1',
      evaluated_at: '2026-05-17T14:32:01Z',
      member_count: 1,
      members: [{ id: 'p1', name: 'Smith, John', gender: 'male', birth_date: '1980-04-12' }],
    });
    renderAt();
    const btn = await screen.findByRole('button', { name: /\$evaluate/i });
    await userEvent.click(btn);
    expect(await screen.findByText('Smith, John')).toBeInTheDocument();
    expect(screen.getByText('1980-04-12')).toBeInTheDocument();
  });

  test('disables button while evaluating', async () => {
    enableAndOneGroup();
    let resolve;
    api.evaluateGroup = jest.fn().mockReturnValue(new Promise(r => { resolve = r; }));
    renderAt();
    const btn = await screen.findByRole('button', { name: /\$evaluate/i });
    await userEvent.click(btn);
    expect(btn).toBeDisabled();
    resolve({ group_id: 'g1', evaluated_at: 't', member_count: 0, members: [] });
  });

  test('renders OperationOutcome on error', async () => {
    enableAndOneGroup();
    const err = new Error('boom');
    err.body = {
      detail: {
        operation_outcome: {
          resourceType: 'OperationOutcome',
          issue: [{ severity: 'error', code: 'not-supported', diagnostics: 'No $evaluate here' }],
        },
      },
    };
    api.evaluateGroup = jest.fn().mockRejectedValue(err);
    renderAt();
    await userEvent.click(await screen.findByRole('button', { name: /\$evaluate/i }));
    expect(await screen.findByText(/No \$evaluate here/i)).toBeInTheDocument();
  });

  test('shows zero-members state when evaluation returns empty member list', async () => {
    enableAndOneGroup();
    api.evaluateGroup = jest.fn().mockResolvedValue({
      group_id: 'g1', evaluated_at: 't', member_count: 0, members: [],
    });
    renderAt();
    await userEvent.click(await screen.findByRole('button', { name: /\$evaluate/i }));
    expect(await screen.findByText(/0 members/i)).toBeInTheDocument();
  });
});
