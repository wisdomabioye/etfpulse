import { TELEGRAM_GROUP_URL } from '../../lib/links';
import { Logo } from './Logo';

export function Footer() {
  return (
    <footer className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4 px-6 sm:px-8 py-8 border-t border-border-2 text-[11px] font-mono text-text-3">
      <div className="flex items-center gap-4">
        <Logo size={12} plain />
        <span className="text-text-4">v0.1.0</span>
      </div>
      <div className="flex gap-5">
        <a href="#" className="text-text-2 hover:text-text-1">GitHub</a>
        <a
          href={TELEGRAM_GROUP_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="text-text-2 hover:text-text-1"
        >
          Telegram
        </a>
        <a href="#" className="text-text-2 hover:text-text-1">How it works</a>
      </div>
    </footer>
  );
}
