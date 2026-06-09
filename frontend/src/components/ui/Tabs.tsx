export interface TabItem<T extends string> {
  value: T;
  label: string;
  /** Optional dim suffix (e.g. a count) shown after the label. */
  sub?: string;
}

interface TabsProps<T extends string> {
  tabs: ReadonlyArray<TabItem<T>>;
  value: T;
  onChange: (value: T) => void;
  size?: 'sm' | 'md';
}

/**
 * Segmented tab strip — ported from the prototype's `Tabs`. Controlled:
 * the parent owns `value` + `onChange`. Generic over the value union so
 * call sites stay type-safe (e.g. `Tabs<TimeHorizon>`). The active tab
 * uses `bg-3` + `line-3`; inactive are transparent `t3`.
 */
export function Tabs<T extends string>({ tabs, value, onChange, size = 'md' }: TabsProps<T>) {
  const padCls = size === 'sm' ? 'px-[11px] py-[5px]' : 'px-[14px] py-[7px]';
  return (
    <div
      role="tablist"
      className="inline-flex gap-[3px] p-[3px] bg-bg-1 border border-line-2 rounded-md"
    >
      {tabs.map((t) => {
        const active = t.value === value;
        return (
          <button
            key={t.value}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(t.value)}
            className={`${padCls} rounded-sm font-mono text-[12px] tracking-[0.03em] transition-[background-color,color,border-color] duration-[var(--dur-1)] border ${
              active
                ? 'bg-bg-3 text-t1 border-line-3 font-semibold'
                : 'bg-transparent text-t3 border-transparent font-normal'
            }`}
          >
            {t.label}
            {t.sub && <span className="text-t4 ml-1.5">{t.sub}</span>}
          </button>
        );
      })}
    </div>
  );
}
