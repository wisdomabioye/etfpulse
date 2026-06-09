import { Link } from 'react-router-dom';

import { TELEGRAM_GROUP_URL } from '../../lib/links';
import { Logo } from '../ui';

interface FooterLink {
  label: string;
  /** Internal route (Link) when starting with "/", else external href. */
  to: string;
}

const COLUMNS: Array<{ heading: string; links: FooterLink[] }> = [
  {
    heading: 'Product',
    links: [
      { label: 'Signals', to: '/signals' },
      { label: 'Proof', to: '/track-record' },
      { label: 'Regime', to: '/regime' },
      { label: 'Trade', to: '/execute' },
    ],
  },
  {
    heading: 'Learn',
    links: [
      { label: 'Methodology', to: '/methodology' },
      { label: 'How it works', to: '/methodology' },
      { label: 'GitHub', to: 'https://github.com' },
      { label: 'Telegram', to: TELEGRAM_GROUP_URL },
    ],
  },
];

function FooterLinkItem({ link }: { link: FooterLink }) {
  const cls = 'text-[13px] text-t2 hover:text-t1 transition-colors';
  if (link.to.startsWith('/')) {
    return (
      <Link to={link.to} className={cls}>
        {link.label}
      </Link>
    );
  }
  return (
    <a href={link.to} target="_blank" rel="noopener noreferrer" className={cls}>
      {link.label}
    </a>
  );
}

/**
 * Site footer — reskinned (R3) to the prototype: brand + tagline, Product /
 * Learn link columns, and the standing disclaimer ("you sign every action,
 * no auto-trading") that is load-bearing for the no-key-custody story.
 */
export function Footer() {
  return (
    <footer className="border-t border-line-2 mt-16 px-6 py-9 bg-bg-1">
      <div className="max-w-[1200px] mx-auto flex flex-wrap gap-6 justify-between items-start">
        <div className="max-w-[300px]">
          <Logo size={14} />
          <p className="text-t3 text-[12px] leading-[1.6] mt-3">
            Bound the loss. Let the rest compound. Calibrated crypto-ETF flow intelligence, scored
            against real prices.
          </p>
        </div>
        <div className="flex gap-12 flex-wrap">
          {COLUMNS.map((col) => (
            <div key={col.heading}>
              <div className="font-mono text-[10px] text-t4 tracking-[0.12em] uppercase mb-3">
                {col.heading}
              </div>
              <div className="flex flex-col gap-[9px]">
                {col.links.map((link) => (
                  <FooterLinkItem key={link.label} link={link} />
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
      <div className="max-w-[1200px] mx-auto mt-7 pt-5 border-t border-line-1 flex justify-between flex-wrap gap-3">
        <span className="font-mono text-[10px] text-t4 uppercase tracking-[0.1em]">
          Not financial advice · You sign every action · No auto-trading
        </span>
        <span className="font-mono text-[10px] text-t4">© 2026 ETFPulse</span>
      </div>
    </footer>
  );
}
