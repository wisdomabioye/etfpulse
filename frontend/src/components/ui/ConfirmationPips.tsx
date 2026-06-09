interface ConfirmationPipsProps {
  /** Confirmation score 0–3 (PR I.2 multi-factor voting). */
  value: number;
  /** Square px size of each pip. */
  size?: number;
}

/**
 * Three-pip confirmation meter (0–3) — ported from the prototype's
 * `ConfirmationPips`. Filled pips use the amber accent, empty use `line-3`.
 * `title` carries the screen-reader/hover meaning.
 */
export function ConfirmationPips({ value, size = 5 }: ConfirmationPipsProps) {
  return (
    <span
      className="inline-flex items-center gap-[3px]"
      title={`Confirmation ${value}/3`}
    >
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className={`rounded-[1.5px] ${i < value ? 'bg-acc' : 'bg-line-3'}`}
          style={{ width: size, height: size }}
        />
      ))}
    </span>
  );
}
