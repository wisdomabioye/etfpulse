interface SegProps<V extends string> {
  /** `[label, value]` pairs, rendered left→right. */
  options: ReadonlyArray<readonly [string, V]>;
  value: V;
  onChange: (value: V) => void;
  className?: string;
}

/**
 * Segmented toggle — ported from the prototype's `Seg`. A pill rail
 * (`bg-1` / `line-2`) with the active option lifted to `bg-3` + `line-3`.
 * Each segment is a real `<button aria-pressed>` so it's keyboard-operable
 * and queryable by role + name.
 */
export function Seg<V extends string>({ options, value, onChange, className = '' }: SegProps<V>) {
  return (
    <div
      className={`flex gap-1 p-[3px] bg-bg-1 border border-line-2 rounded-sm ${className}`.trim()}
    >
      {options.map(([label, v]) => {
        const active = value === v;
        return (
          <button
            key={v}
            type="button"
            aria-pressed={active}
            onClick={() => onChange(v)}
            className={`flex-1 py-[7px] rounded-[4px] font-mono text-[11px] border transition-colors duration-[var(--dur-1)] ${
              active
                ? 'bg-bg-3 text-t1 border-line-3'
                : 'bg-transparent text-t3 border-transparent hover:text-t2'
            }`}
          >
            {label}
          </button>
        );
      })}
    </div>
  );
}
