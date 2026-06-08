/**
 * Methodology page tests (#190).
 *
 * Coverage targets:
 *   - Page renders without crash inside a router context.
 *   - Eight section headings render in the documented order — locks
 *     structural drift (adding/removing/reordering a section fails
 *     loudly).
 *   - Table of contents has one entry per section, every TOC anchor
 *     points to a section id that exists in the body.
 *   - TOC numbering matches the `01..08` zero-padded format the page
 *     promises elsewhere in the app.
 *   - Every cross-link (in-body `<Link>`) targets a route on the
 *     allow-list defined here — catches typos like `/track_record`
 *     vs `/track-record` before they ship as broken anchors. Allow-list
 *     is intentionally Methodology-specific (not a mirror of every
 *     route in App.tsx) so the test fails when Methodology drifts but
 *     stays insulated from unrelated route additions elsewhere.
 *   - Negative test: no console.error during render — catches react-
 *     router warnings (broken Link contexts, hydration mismatches).
 */

import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { METHODOLOGY_SECTIONS } from '../components/methodology';

import { Methodology } from './Methodology';

/**
 * Routes Methodology is allowed to cross-link to. Drift-detector for the
 * page's own anchors — see file header. Keep narrow on purpose; adding a
 * cross-link to a new page requires updating both this set and the
 * sections content in a single change.
 */
const EXPECTED_CROSS_LINK_TARGETS = new Set([
  '/track-record',
  '/signals',
  '/regime',
  '/analytics',
]);

/**
 * Snapshot of the shipped section IDs in order. Pins structure so an
 * accidental reorder, addition, or removal fails the test loudly —
 * intentional changes require updating both this list and the data
 * file in the same PR.
 */
const EXPECTED_SECTION_IDS = [
  'data-sources',
  'detectors',
  'confidence',
  'confirmation',
  'market-composite',
  'horizon-rubric',
  'what-its-not',
  'caveats',
] as const;

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/methodology']}>
      <Methodology />
    </MemoryRouter>,
  );
}

describe('Methodology page', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the page header', () => {
    renderPage();
    expect(
      screen.getByRole('heading', { level: 1, name: /methodology/i }),
    ).toBeInTheDocument();
  });

  it('ships exactly the expected section IDs in order', () => {
    expect(METHODOLOGY_SECTIONS.map((s) => s.id)).toEqual([
      ...EXPECTED_SECTION_IDS,
    ]);
  });

  it('renders all section headings in the documented order', () => {
    renderPage();
    const h2s = screen.getAllByRole('heading', { level: 2 });
    const expectedTitles = METHODOLOGY_SECTIONS.map((s) => s.title);
    expect(h2s).toHaveLength(EXPECTED_SECTION_IDS.length);
    expect(h2s.map((el) => el.textContent)).toEqual(expectedTitles);
  });

  it('TOC has one entry per section', () => {
    renderPage();
    const nav = screen.getByRole('navigation', { name: /on this page/i });
    const links = within(nav).getAllByRole('link');
    expect(links).toHaveLength(EXPECTED_SECTION_IDS.length);
  });

  it('every TOC anchor targets an existing section id', () => {
    renderPage();
    const nav = screen.getByRole('navigation', { name: /on this page/i });
    const links = within(nav).getAllByRole('link');
    links.forEach((link, i) => {
      const href = link.getAttribute('href');
      expect(href).toBe(`#${METHODOLOGY_SECTIONS[i].id}`);
      expect(document.getElementById(METHODOLOGY_SECTIONS[i].id)).not.toBeNull();
    });
  });

  it('TOC numbering is two-digit zero-padded (01..08)', () => {
    renderPage();
    const nav = screen.getByRole('navigation', { name: /on this page/i });
    const items = within(nav).getAllByRole('listitem');
    items.forEach((item, i) => {
      const expected = (i + 1).toString().padStart(2, '0');
      expect(item.textContent).toContain(expected);
    });
  });

  it('every in-body cross-link targets an allow-listed route', () => {
    renderPage();
    const nav = screen.getByRole('navigation', { name: /on this page/i });
    const tocLinks = new Set(within(nav).getAllByRole('link'));

    const allLinks = screen.getAllByRole('link');
    const bodyLinks = allLinks.filter((l) => !tocLinks.has(l));

    expect(bodyLinks.length).toBeGreaterThan(0);
    bodyLinks.forEach((link) => {
      const href = link.getAttribute('href');
      expect(href).not.toBeNull();
      expect(EXPECTED_CROSS_LINK_TARGETS.has(href as string)).toBe(true);
    });
  });

  it('does not emit console errors or warnings during render', () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    renderPage();
    expect(errorSpy).not.toHaveBeenCalled();
    expect(warnSpy).not.toHaveBeenCalled();
  });
});
