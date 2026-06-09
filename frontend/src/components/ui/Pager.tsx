import { Button } from './Button';

interface PagerProps {
  /** 1-based current page number. */
  page: number;
  /** Total number of pages. <= 1 → component renders nothing. */
  totalPages: number;
  onPageChange: (next: number) => void;
  /** Visible window size around the current page (each side). Default 1 →
   * shows pages [current-1, current, current+1] plus first/last + ellipses. */
  siblingCount?: number;
  className?: string;
}

const DOTS = '…';

type PageItem = number | typeof DOTS;

/**
 * Numeric pager — Prev · 1 · … · 4 · 5 · 6 · … · 12 · Next.
 *
 * Stateless: the consumer owns `page` (typically component or URL state) and
 * receives `(next: number) => void` on click. The component refuses to render
 * when there's nothing meaningful to navigate (`totalPages <= 1`) so the
 * caller doesn't need a guard at the call site.
 *
 * `siblingCount=1` keeps the bar narrow enough for ~10-page typical use; bump
 * to 2 for very long lists to surface more nearby pages without scrolling.
 *
 * Accessibility: `aria-current="page"` on the active button; arrow keys are
 * NOT trapped (browser tab traversal works as-is).
 */
export function Pager({
  page,
  totalPages,
  onPageChange,
  siblingCount = 1,
  className = '',
}: PagerProps) {
  if (totalPages <= 1) return null;

  const items = buildPageWindow(page, totalPages, siblingCount);
  const canPrev = page > 1;
  const canNext = page < totalPages;

  return (
    <nav
      aria-label="Pagination"
      className={`flex items-center justify-center gap-1.5 ${className}`.trim()}
    >
      <Button
        as="button"
        variant="secondary"
        size="sm"
        onClick={() => onPageChange(page - 1)}
        disabled={!canPrev}
        aria-label="Previous page"
      >
        Prev
      </Button>

      {items.map((item, idx) =>
        item === DOTS ? (
          <span
            key={`dots-${idx}`}
            className="px-2 font-mono text-[12px] text-t3 select-none"
            aria-hidden
          >
            {DOTS}
          </span>
        ) : (
          <PagerButton
            key={item}
            n={item}
            active={item === page}
            onClick={() => onPageChange(item)}
          />
        ),
      )}

      <Button
        as="button"
        variant="secondary"
        size="sm"
        onClick={() => onPageChange(page + 1)}
        disabled={!canNext}
        aria-label="Next page"
      >
        Next
      </Button>
    </nav>
  );
}

interface PagerButtonProps {
  n: number;
  active: boolean;
  onClick: () => void;
}

function PagerButton({ n, active, onClick }: PagerButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? 'page' : undefined}
      aria-label={`Page ${n}`}
      className={`min-w-[34px] px-2.5 py-[6px] rounded-[5px] text-[12px] font-medium font-mono tracking-[0.02em] border transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-acc ${
        active
          ? 'bg-acc text-bg-1 border-acc'
          : 'bg-transparent text-t2 border-line-2 hover:text-t1 hover:border-line-3'
      }`}
    >
      {n}
    </button>
  );
}

/**
 * Compute the visible page tokens for the bar. Pure function — exported as
 * an internal helper for unit testing.
 *
 * Examples (siblings=1):
 *   page=1, total=10  → 1 2 3 … 10
 *   page=5, total=10  → 1 … 4 5 6 … 10
 *   page=9, total=10  → 1 … 8 9 10
 *   page=3, total=4   → 1 2 3 4   (no ellipses needed for short ranges)
 */
function buildPageWindow(
  page: number,
  totalPages: number,
  siblingCount: number,
): PageItem[] {
  // 5 = first + last + current + 2 ellipsis slots; if total fits, just show
  // every page rather than introduce ellipses.
  const totalNumbers = siblingCount * 2 + 5;
  if (totalPages <= totalNumbers) {
    return range(1, totalPages);
  }

  const leftSibling = Math.max(page - siblingCount, 1);
  const rightSibling = Math.min(page + siblingCount, totalPages);

  const showLeftDots = leftSibling > 2;
  const showRightDots = rightSibling < totalPages - 1;

  if (!showLeftDots && showRightDots) {
    // 1 2 3 4 5 … N — current is near the start.
    const leftRange = range(1, 3 + siblingCount * 2);
    return [...leftRange, DOTS, totalPages];
  }
  if (showLeftDots && !showRightDots) {
    // 1 … N-4 N-3 N-2 N-1 N — current is near the end.
    const rightRange = range(totalPages - (3 + siblingCount * 2) + 1, totalPages);
    return [1, DOTS, ...rightRange];
  }
  // 1 … L L+1 … R R+1 … N — current is in the middle.
  const middle = range(leftSibling, rightSibling);
  return [1, DOTS, ...middle, DOTS, totalPages];
}

function range(from: number, to: number): number[] {
  const out: number[] = [];
  for (let n = from; n <= to; n++) out.push(n);
  return out;
}
