/**
 * `<IdInput>` — numeric id input with consistent styling (#187).
 *
 * Centralises the styling + accessibility for the user_id / signal_id /
 * group_id text inputs used across admin sections. Parent owns the
 * string state + does validation via `parsePositiveId` (lib/parseId.ts);
 * this component is presentation-only.
 *
 * `placeholder` + `ariaLabel` are required so screen readers + e2e
 * tests can target the input deterministically. Default `width='w-28'`
 * fits 6-digit ids comfortably; pass `'w-32'` for signal-id inputs that
 * may carry a leading hash mark visually.
 */

interface Props {
  value: string;
  onChange: (next: string) => void;
  placeholder: string;
  ariaLabel: string;
  disabled?: boolean;
  /** Tailwind width class. Default `w-28` (fits 6+ digits). */
  width?: string;
}

export function IdInput({
  value,
  onChange,
  placeholder,
  ariaLabel,
  disabled = false,
  width = 'w-28',
}: Props) {
  return (
    <input
      type="text"
      inputMode="numeric"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      aria-label={ariaLabel}
      disabled={disabled}
      className={
        `${width} bg-bg-3 text-t1 border border-line-3 rounded-[5px] ` +
        'px-3 py-2 text-[13px] font-mono focus-visible:outline-none ' +
        'focus-visible:ring-2 focus-visible:ring-acc disabled:opacity-50'
      }
    />
  );
}
