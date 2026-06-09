import { Link } from 'react-router-dom';

export interface Crumb {
  label: string;
  /** When set, the crumb is a link; otherwise it's the current (plain) page. */
  path?: string;
}

interface BreadcrumbProps {
  items: Crumb[];
  className?: string;
}

/**
 * Mono breadcrumb trail — ported from the prototype's `Breadcrumb`. Linked
 * crumbs use `t3`, the trailing current crumb `t2`, separated by a `t4` slash.
 */
export function Breadcrumb({ items, className = '' }: BreadcrumbProps) {
  return (
    <div className={`flex items-center gap-2 mb-[18px] font-mono text-[11px] ${className}`.trim()}>
      {items.map((it, i) => (
        <span key={`${it.label}-${i}`} className="flex items-center gap-2">
          {i > 0 && <span className="text-t4">/</span>}
          {it.path ? (
            <Link to={it.path} className="text-t3 hover:text-t1">
              {it.label}
            </Link>
          ) : (
            <span className="text-t2">{it.label}</span>
          )}
        </span>
      ))}
    </div>
  );
}
