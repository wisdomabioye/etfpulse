import { METHODOLOGY_SECTIONS, Section } from '../components/methodology';
import { Kicker, PageHeader } from '../components/ui';

/**
 * /methodology — long-form explanation of what the published numbers
 * mean and what they DON'T. The single canonical home for the
 * caveats that appear in shorter form on /track-record's inline
 * footnote. When the inline caveats and this page disagree, this
 * page is the source of truth.
 *
 * Page is a thin renderer over {@link METHODOLOGY_SECTIONS} — the
 * sections list drives both the table-of-contents and the rendered
 * body, so the two can't drift. To add a section: edit the array
 * in `components/methodology/sections.tsx` only.
 */
export function Methodology() {
  return (
    <div className="max-w-[920px] mx-auto px-6 sm:px-8 pt-8 pb-16">
      <PageHeader
        eyebrow={
          <Kicker dot dotColor="info">
            How it works · what the numbers mean
          </Kicker>
        }
        title="Methodology"
      />

      <p className="mt-4 text-text-2 leading-[1.6] max-w-[700px]">
        Eight short sections covering where the data comes from, how
        signals are produced, how they're scored, and the specific
        things our published numbers do not mean.
      </p>

      <nav
        aria-label="On this page"
        className="mt-6 rounded-lg border border-border-2 bg-bg-2 px-5 py-4"
      >
        <div className="font-mono text-[10px] text-text-3 uppercase tracking-[0.12em] mb-2.5">
          On this page
        </div>
        <ol className="grid grid-cols-1 sm:grid-cols-2 gap-y-1.5 gap-x-6 text-[13px] text-text-2">
          {METHODOLOGY_SECTIONS.map((s, i) => (
            <li key={s.id}>
              <a
                href={`#${s.id}`}
                className="hover:text-text-1 transition-colors"
              >
                <span className="font-mono text-text-4 mr-2">
                  {formatN(i + 1)}
                </span>
                {s.title}
              </a>
            </li>
          ))}
        </ol>
      </nav>

      {METHODOLOGY_SECTIONS.map((s, i) => (
        <Section key={s.id} id={s.id} n={formatN(i + 1)} title={s.title}>
          {s.body}
        </Section>
      ))}
    </div>
  );
}

/** Two-digit zero-padded section index. Mirrors the "01 / NAME"
 *  pattern SectionLabel renders elsewhere in the app. */
function formatN(i: number): string {
  return i.toString().padStart(2, '0');
}
