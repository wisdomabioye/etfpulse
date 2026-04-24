import { Logo } from './Logo';

/**
 * Minimal footer — Logo + version + external pointers.
 *
 * Mock shows GitHub / Telegram / "How it works" links. For MVP these
 * are rendered but point to `#` placeholders; wire up to real URLs in
 * Stage 6e polish (#78) or via env config.
 */
export function Footer() {
  return (
    <footer className="mt-24 border-t border-border-2 px-5 md:px-7 py-8 md:py-10 flex flex-col md:flex-row items-start md:items-center justify-between gap-4 text-[11px] font-mono text-text-3">
      <div className="flex items-center gap-4">
        <Logo size={12} plain />
        <span className="text-text-4">v0.1.0</span>
      </div>
      <div className="flex gap-5">
        <a href="#" className="text-text-2 no-underline hover:text-text-1">
          GitHub
        </a>
        <a href="#" className="text-text-2 no-underline hover:text-text-1">
          Telegram
        </a>
        <a href="#" className="text-text-2 no-underline hover:text-text-1">
          How it works
        </a>
      </div>
    </footer>
  );
}
