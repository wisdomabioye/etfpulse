import { useMemo, useState } from 'react';
import type { FormEvent } from 'react';

import type {
  BacktestDetector,
  BacktestDetectorParam,
  BacktestRequest,
  DetectorKwargValue,
} from '../../api/backtest';
import { Button, Skeleton } from '../ui';

interface BacktestFormProps {
  detectors: BacktestDetector[] | undefined;
  /** True while `useBacktestDetectors` is still resolving. Skeletons
   *  cover the form area instead of an empty grid. */
  detectorsLoading: boolean;
  /** Disables the submit button. Caller sets while the mutation is
   *  in flight so a double-click can't fire a second sweep. */
  busy: boolean;
  /** Called with a ready-to-fire BacktestRequest. */
  onSubmit: (request: BacktestRequest) => void;
}

/** Default lookback window for a fresh form. 30 days is below the
 *  90-day backend cap and small enough that a typical sweep completes
 *  in under a minute. Operator can extend up to the cap. */
const DEFAULT_LOOKBACK_DAYS = 30;

/**
 * Form for `POST /api/admin/backtest`. Drives:
 *   - start + end date inputs
 *   - per-detector kwarg overrides (one row per param from the
 *     `/detectors` listing — labeled inputs typed by `param.type_name`)
 *   - allow_ai opt-in toggle with explicit cost warning
 *
 * State is local to this component. The page just hands us a submit
 * handler.
 */
export function BacktestForm({
  detectors,
  detectorsLoading,
  busy,
  onSubmit,
}: BacktestFormProps) {
  const [start, setStart] = useState<string>(defaultStartDate());
  const [end, setEnd] = useState<string>(defaultEndDate());
  const [overrides, setOverrides] = useState<
    Record<string, Record<string, string>>
  >({});
  const [allowAI, setAllowAI] = useState(false);

  const updateOverride = (
    detectorName: string,
    paramName: string,
    value: string,
  ) => {
    setOverrides((prev) => {
      const next = { ...prev };
      const slot = { ...(next[detectorName] ?? {}) };
      if (value === '') {
        delete slot[paramName];
      } else {
        slot[paramName] = value;
      }
      if (Object.keys(slot).length === 0) {
        delete next[detectorName];
      } else {
        next[detectorName] = slot;
      }
      return next;
    });
  };

  const detector_overrides = useMemo(
    () => coerceOverrides(overrides, detectors),
    [overrides, detectors],
  );

  const handle = (e: FormEvent) => {
    e.preventDefault();
    // Disabled submit button blocks clicks, but pressing Enter inside
    // a date input still fires the form's submit event. Without this
    // guard a second backtest would fire mid-run, hammering the
    // backend with a duplicate request.
    if (busy) return;
    const request: BacktestRequest = {
      start,
      end,
      ...(Object.keys(detector_overrides).length > 0 && { detector_overrides }),
      ...(allowAI && { allow_ai: true }),
    };
    onSubmit(request);
  };

  return (
    <form
      onSubmit={handle}
      className="border border-border-2 bg-bg-2 rounded-md p-5 space-y-5"
    >
      <fieldset className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <label className="block">
          <span className="block font-mono text-[10px] text-text-3 uppercase tracking-[0.1em] mb-1.5">
            Start date
          </span>
          <input
            type="date"
            required
            value={start}
            onChange={(e) => setStart(e.target.value)}
            className={INPUT_CLASS}
          />
        </label>
        <label className="block">
          <span className="block font-mono text-[10px] text-text-3 uppercase tracking-[0.1em] mb-1.5">
            End date
          </span>
          <input
            type="date"
            required
            value={end}
            onChange={(e) => setEnd(e.target.value)}
            className={INPUT_CLASS}
          />
        </label>
      </fieldset>

      <section className="space-y-3">
        <h3 className="font-mono text-[11px] text-text-3 uppercase tracking-[0.1em]">
          Detector overrides — leave blank to use production defaults
        </h3>
        {detectorsLoading || !detectors ? (
          <Skeleton className="h-32" />
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {detectors.map((d) => (
              <DetectorOverrideCard
                key={d.name}
                detector={d}
                values={overrides[d.name] ?? {}}
                onChange={updateOverride}
              />
            ))}
          </div>
        )}
      </section>

      <label className="flex items-start gap-3 text-[13px] text-text-2">
        <input
          type="checkbox"
          checked={allowAI}
          onChange={(e) => setAllowAI(e.target.checked)}
          className="mt-0.5"
        />
        <span>
          Allow live AI calls on cache miss.{' '}
          <span className="text-text-4">
            Charged against the OpenRouter daily cap; today the live
            caller is unwired so this flag is a no-op.
          </span>
        </span>
      </label>

      <div className="flex items-center justify-end gap-3">
        <Button type="submit" variant="primary" disabled={busy}>
          {busy ? 'Running…' : 'Run backtest'}
        </Button>
      </div>
    </form>
  );
}

const INPUT_CLASS =
  'w-full bg-bg-3 text-text-1 border border-border-3 rounded-[5px] px-3 py-2 text-[13px] font-mono focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent';

interface DetectorOverrideCardProps {
  detector: BacktestDetector;
  values: Record<string, string>;
  onChange: (detectorName: string, paramName: string, value: string) => void;
}

function DetectorOverrideCard({
  detector,
  values,
  onChange,
}: DetectorOverrideCardProps) {
  return (
    <div className="border border-border-2 rounded-md p-3.5 space-y-2.5">
      <div className="font-medium text-text-1 text-[13px]">{detector.name}</div>
      <div className="space-y-2">
        {detector.params.map((p) => (
          <label key={p.name} className="block">
            <span className="block text-[11px] text-text-3 mb-1 font-mono">
              {p.name}
              <span className="text-text-4 ml-1">
                · {p.type_name}
                {p.has_default && p.default !== null
                  ? ` · default ${String(p.default)}`
                  : ''}
              </span>
            </span>
            <input
              type={inputTypeForParam(p)}
              step={p.type_name === 'int' ? 1 : undefined}
              value={values[p.name] ?? ''}
              onChange={(e) => onChange(detector.name, p.name, e.target.value)}
              placeholder={
                p.has_default && p.default !== null ? String(p.default) : ''
              }
              className={INPUT_CLASS}
            />
          </label>
        ))}
      </div>
    </div>
  );
}

function inputTypeForParam(param: BacktestDetectorParam): string {
  if (param.type_name === 'bool') return 'text'; // operator types true/false
  // int + float + Decimal all render as native number inputs. Browsers
  // gate to numeric input modes on mobile and reject obviously-broken
  // typeahead. Decimal precision is preserved by the backend's
  // `Decimal(str(v))` coercion in `_build_detector`.
  if (
    param.type_name === 'int' ||
    param.type_name === 'float' ||
    param.type_name === 'Decimal'
  ) {
    return 'number';
  }
  return 'text';
}

/** Coerce string-form override values back into `DetectorKwargValue`s
 *  matching the param's declared type. Empty strings are filtered by
 *  `updateOverride` before this runs. Unknown / malformed values fall
 *  through as the raw string so the backend can surface a 422 with a
 *  useful detail message rather than the FE silently dropping them. */
function coerceOverrides(
  raw: Record<string, Record<string, string>>,
  detectors: BacktestDetector[] | undefined,
): Record<string, Record<string, DetectorKwargValue>> {
  if (!detectors) return {};
  const out: Record<string, Record<string, DetectorKwargValue>> = {};
  for (const [detectorName, paramValues] of Object.entries(raw)) {
    const params = detectors.find((d) => d.name === detectorName)?.params;
    if (!params) continue;
    const slot: Record<string, DetectorKwargValue> = {};
    for (const [paramName, str] of Object.entries(paramValues)) {
      const meta = params.find((p) => p.name === paramName);
      const typeName = meta?.type_name;
      if (typeName === 'int') {
        const n = Number.parseInt(str, 10);
        slot[paramName] = Number.isFinite(n) ? n : str;
      } else if (typeName === 'float' || typeName === 'Decimal') {
        const n = Number.parseFloat(str);
        slot[paramName] = Number.isFinite(n) ? n : str;
      } else if (typeName === 'bool') {
        const lc = str.toLowerCase().trim();
        if (lc === 'true' || lc === 'false') {
          slot[paramName] = lc === 'true';
        } else {
          slot[paramName] = str;
        }
      } else {
        slot[paramName] = str;
      }
    }
    if (Object.keys(slot).length > 0) out[detectorName] = slot;
  }
  return out;
}

function defaultStartDate(): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - DEFAULT_LOOKBACK_DAYS);
  return d.toISOString().slice(0, 10);
}

function defaultEndDate(): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - 1);
  return d.toISOString().slice(0, 10);
}
