import fs from 'fs';
import path from 'path';

// This guard validates only the *literal string* route paths currently used in
// App.js against frontend/public/serve.json's rewrite sources (see #383). It does
// not model full react-router / serve-handler (path-to-regexp + minimatch)
// matching semantics — those two libraries do not share a common route-matching
// grammar. `/results/:jobId` compares equal only because both spell a single
// dynamic segment as `:jobId` today; that's a coincidence of the current route
// set, not a guarantee for future shapes (wildcards, optional segments, nested
// routes). If a route is added that this guard can't parse as a plain
// `path="/literal"` string, the test below fails loudly rather than silently
// under-counting — update this file's regex when that happens.

describe('App — route / serve.json rewrite parity (#383)', () => {
  const appSource = fs.readFileSync(path.join(__dirname, 'App.js'), 'utf8');
  const serveConfig = JSON.parse(
    fs.readFileSync(path.join(__dirname, '..', 'public', 'serve.json'), 'utf8'),
  );

  const routeTagCount = (appSource.match(/<Route\b/g) || []).length;
  const routePaths = [...appSource.matchAll(/<Route\b[^>]*\bpath="([^"]*)"/g)].map(
    (m) => m[1],
  );

  test('every <Route> in App.js has a literal, parseable path="..." attribute', () => {
    // If this fails, a route was added with a non-literal path (a constant,
    // single quotes, an expression) that the regex above can't see. Fix the
    // regex rather than ignoring the failure — an unparsed route is exactly
    // how #383 happened.
    expect(routePaths.length).toBe(routeTagCount);
  });

  test('at least 8 routes are registered', () => {
    // Guards against an App.js refactor (e.g. moving routes into a config
    // object) silently reducing this file's extraction to zero routes, which
    // would make the parity checks below vacuously pass.
    expect(routePaths.length).toBeGreaterThanOrEqual(8);
  });

  const appRouteSet = new Set(routePaths.filter((p) => !p.includes('*')));
  const serveSourceSet = new Set(
    (serveConfig.rewrites || [])
      .map((r) => r.source)
      .filter((s) => !s.includes('*')),
  );

  test('every non-splat App.js route has a matching serve.json rewrite', () => {
    const missing = [...appRouteSet].filter((p) => !serveSourceSet.has(p));
    expect(missing).toEqual([]);
  });

  test('every serve.json rewrite corresponds to a real App.js route (no dead entries)', () => {
    const dead = [...serveSourceSet].filter((s) => !appRouteSet.has(s));
    expect(dead).toEqual([]);
  });
});
