import { SCORING_SECTIONS } from './sections-scoring';
import { SIGNAL_SECTIONS } from './sections-signals';
import type { SectionContent } from './sectionTypes';

/**
 * Single source of truth for the methodology page's structure. The
 * page's table-of-contents and the rendered body both iterate this
 * array, so a new section requires exactly one edit (in the matching
 * sections-*.tsx data file). Section ORDER is load-bearing — the
 * page numbers sections 01..08 and tests pin the id order.
 */

export type { SectionContent } from './sectionTypes';

export const METHODOLOGY_SECTIONS: SectionContent[] = [
  ...SIGNAL_SECTIONS,
  ...SCORING_SECTIONS,
];
