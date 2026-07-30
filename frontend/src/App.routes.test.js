import fs from 'fs';
import path from 'path';
import { ROUTES } from './routes';

// This guard validates real route data (ROUTES, from ./routes.js) against
// public/serve.json's rewrite list, plus a source-text check that App.js has
// no route left un-derived from ROUTES (see #383, #384). It does not model
// full react-router / serve-handler (path-to-regexp + minimatch) matching
// semantics — those two libraries do not share a common route-matching
// grammar. `/results/:jobId` compares equal only because both spell a single
// dynamic segment as `:jobId` today; that's a coincidence of the current
// route set, not a guarantee for future shapes (wildcards, optional
// segments, nested routes).

describe('App — route / serve.json rewrite parity (#383, #384)', () => {
  const appSource = fs.readFileSync(path.join(__dirname, 'App.js'), 'utf8');
  const serveConfig = JSON.parse(
    fs.readFileSync(path.join(__dirname, '..', 'public', 'serve.json'), 'utf8'),
  );

  test('App.js has no hand-written literal-path <Route> — every route comes from ROUTES', () => {
    // If this fails, someone added a route straight into the JSX instead of
    // to ROUTES, bypassing the sidebar/title/placeholder derivation *and*
    // this parity guard — exactly how #383 happened. Matches any literal
    // path spelling (`path="/foo"`, `path='/foo'`, `path={'/foo'}`,
    // `path={"/foo"}`, `` path={`/foo`} ``) but not App.js's own legitimate
    // `path={path}` (an identifier reference into the ROUTES.map loop) or
    // MenuIcon's unrelated inline `<path d="...">` SVG element.
    const literalRoutePaths = [...appSource.matchAll(
      /<Route\b[^>]*\bpath=(?:"[^"]*"|'[^']*'|\{\s*(?:"[^"]*"|'[^']*'|`[^`]*`)\s*\})/g,
    )];
    expect(literalRoutePaths).toEqual([]);
  });

  test('at least 8 routes are registered', () => {
    // Guards against ROUTES being silently gutted, which would make the
    // parity checks below vacuously pass.
    expect(ROUTES.length).toBeGreaterThanOrEqual(8);
  });

  test('every route path is unique', () => {
    const paths = ROUTES.map((r) => r.path);
    expect(new Set(paths).size).toBe(paths.length);
  });

  test('every nav route has a unique keyboard shortcut (case-insensitive, matching App.js)', () => {
    // App.js matches shortcuts case-insensitively (`kbd.toLowerCase() ===
    // e.key.toLowerCase()`), so 'M' and 'm' would collide there even though
    // they're distinct strings — normalize before comparing or this guard
    // misses that ambiguity.
    const kbds = ROUTES.filter((r) => r.nav).map((r) => r.nav.kbd.toLowerCase());
    expect(new Set(kbds).size).toBe(kbds.length);
  });

  test('every non-redirect route resolves a title and searchPlaceholder via basePath', () => {
    // Title/placeholder are looked up by basePath in App.js, which is why
    // /results/:jobId intentionally carries neither of its own — it must
    // resolve through the /results entry's basePath ('/results').
    const unresolved = ROUTES.filter((r) => !r.redirectTo)
      .map((r) => '/' + r.path.split('/')[1])
      .filter((basePath) => {
        const match = ROUTES.find((r) => r.path === basePath);
        return !match?.title || !match?.searchPlaceholder;
      });
    expect(unresolved).toEqual([]);
  });

  const routeSet = new Set(ROUTES.map((r) => r.path).filter((p) => !p.includes('*')));
  const serveSourceSet = new Set(
    (serveConfig.rewrites || [])
      .map((r) => r.source)
      .filter((s) => !s.includes('*')),
  );

  test('every serve.json rewrite destination is /index.html', () => {
    const wrong = (serveConfig.rewrites || []).filter((r) => r.destination !== '/index.html');
    expect(wrong).toEqual([]);
  });

  test('every ROUTES path has a matching serve.json rewrite', () => {
    const missing = [...routeSet].filter((p) => !serveSourceSet.has(p));
    expect(missing).toEqual([]);
  });

  test('every serve.json rewrite corresponds to a real ROUTES path (no dead entries)', () => {
    const dead = [...serveSourceSet].filter((s) => !routeSet.has(s));
    expect(dead).toEqual([]);
  });
});
