import { Link } from 'react-router-dom';

import type { BacktestReport } from '../../api/backtest';

interface BacktestResultsCardProps {
  report: BacktestReport;
}

/**
 * Per-detector results table for a completed backtest run. One row
 * per detector — n_hits / n_scored / wins / losses / hit_rate. Each
 * row cross-links to /track-record (filtered by signal type) so the
 * operator can eyeball backtest vs production numbers side-by-side.
 *
 * Empty/zero cells render as "—" instead of "0" so a noisy reader can
 * tell which detectors are genuinely cold vs which are scoring zero
 * wins out of a real sample. Same convention the per-detector
 * leaderboard uses on /track-record.
 */
export function BacktestResultsCard({ report }: BacktestResultsCardProps) {
  const hasRows = report.per_detector.length > 0;

  return (
    <section
      aria-labelledby="backtest-results-heading"
      className="border border-line-2 bg-bg-2 rounded-md"
    >
      <header className="flex items-baseline justify-between gap-4 px-5 py-4 border-b border-line-2">
        <h2
          id="backtest-results-heading"
          className="text-[15px] font-semibold text-t1"
        >
          Results · {report.start} → {report.end}
        </h2>
        <div className="font-mono text-[11px] text-t3">
          prompt {report.ai_prompt_version}
        </div>
      </header>

      {!hasRows ? (
        <div className="px-5 py-6 text-[13px] text-t3">
          No detector data in the report. Either the window has no
          eligible signals or every detector errored — check the
          server logs for `backtest_route_completed` events.
        </div>
      ) : (
        <table className="w-full text-[13px]">
          <thead>
            <tr className="text-left text-t3 font-mono text-[10px] uppercase tracking-[0.1em] border-b border-line-2">
              <th className="px-5 py-2 font-normal">Detector</th>
              <th className="px-3 py-2 font-normal text-right">Hits</th>
              <th className="px-3 py-2 font-normal text-right">Scored</th>
              <th className="px-3 py-2 font-normal text-right">Wins</th>
              <th className="px-3 py-2 font-normal text-right">Losses</th>
              <th className="px-3 py-2 font-normal text-right">Hit rate</th>
              <th className="px-5 py-2 font-normal text-right">Compare</th>
            </tr>
          </thead>
          <tbody>
            {report.per_detector.map((row) => (
              <tr
                key={row.detector_name}
                className="border-b border-line-2 last:border-b-0"
              >
                <td className="px-5 py-2 text-t1 font-medium">
                  {row.detector_name}
                </td>
                <td className="px-3 py-2 text-right text-t2 font-mono">
                  {row.n_hits || dash()}
                </td>
                <td className="px-3 py-2 text-right text-t2 font-mono">
                  {row.n_scored || dash()}
                </td>
                <td className="px-3 py-2 text-right text-t2 font-mono">
                  {row.wins || dash()}
                </td>
                <td className="px-3 py-2 text-right text-t2 font-mono">
                  {row.losses || dash()}
                </td>
                <td className="px-3 py-2 text-right text-t1 font-mono">
                  {row.hit_rate !== null ? `${(row.hit_rate * 100).toFixed(1)}%` : dash()}
                </td>
                <td className="px-5 py-2 text-right">
                  <Link
                    to="/track-record"
                    className="text-acc hover:text-acc text-[12px] underline"
                    title={`Compare with live ${row.detector_name} hit rate on /track-record`}
                  >
                    live
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function dash() {
  return <span className="text-t4">—</span>;
}
