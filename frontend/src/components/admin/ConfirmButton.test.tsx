/**
 * `<ConfirmButton>` unit tests (#187).
 *
 * The other admin primitives (ActionSection, IdInput, ScopeRadios) are
 * effectively stateless and exercised indirectly by section tests.
 * ConfirmButton owns a state machine (idle → confirming → action fired);
 * pinning it here catches regressions at the primitive boundary instead
 * of via N section tests when something breaks.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { ConfirmButton } from './ConfirmButton';

describe('ConfirmButton', () => {
  it('renders idleLabel button initially', () => {
    render(<ConfirmButton idleLabel="Delete…" confirmLabel="Confirm" onConfirm={() => {}} />);
    expect(screen.getByRole('button', { name: 'Delete…' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument();
  });

  it('clicking idle shows confirm + cancel; cancel reverts to idle', () => {
    render(<ConfirmButton idleLabel="Delete…" confirmLabel="Confirm" onConfirm={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: 'Delete…' }));

    expect(screen.getByRole('button', { name: 'Confirm' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete…' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.getByRole('button', { name: 'Delete…' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument();
  });

  it('confirm click fires onConfirm AND returns to idle', () => {
    const onConfirm = vi.fn();
    render(<ConfirmButton idleLabel="Delete…" confirmLabel="Confirm" onConfirm={onConfirm} />);
    fireEvent.click(screen.getByRole('button', { name: 'Delete…' }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));

    expect(onConfirm).toHaveBeenCalledTimes(1);
    // After confirm fires, component returns to idle state (parent's
    // mutation.isPending controls the disabled state from here).
    expect(screen.getByRole('button', { name: 'Delete…' })).toBeInTheDocument();
  });

  it('disabled=true disables the idle button (idle state)', () => {
    render(
      <ConfirmButton
        idleLabel="Delete…"
        confirmLabel="Confirm"
        onConfirm={() => {}}
        disabled
      />,
    );
    expect(screen.getByRole('button', { name: 'Delete…' })).toBeDisabled();
  });

  it('pending=true ALSO disables the idle button (post-fire state)', () => {
    // After a Confirm click flips the component back to idle, the idle
    // button must STILL be disabled if the parent mutation is in flight
    // — otherwise the operator could re-enter confirming state mid-flight.
    render(
      <ConfirmButton
        idleLabel="Delete…"
        confirmLabel="Confirm"
        onConfirm={() => {}}
        pending
      />,
    );
    expect(screen.getByRole('button', { name: 'Delete…' })).toBeDisabled();
  });

  it('confirm button label reflects dynamic confirmLabel prop', () => {
    // Sections use `Confirm unbind #${userId}` — verify the dynamic
    // value is rendered (not the literal template).
    render(
      <ConfirmButton
        idleLabel="Unbind…"
        confirmLabel="Confirm unbind #42"
        onConfirm={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Unbind…' }));
    expect(screen.getByRole('button', { name: 'Confirm unbind #42' })).toBeInTheDocument();
  });

  it('disabled=true in confirming state disables Confirm (input-cleared-mid-confirm guard)', () => {
    // Bug class: user clicks Unbind with id=42, enters confirming,
    // CHANGES input to empty (parent recomputes disabled=true), clicks
    // Confirm — without this gate, the parent's onConfirm would
    // short-circuit on the invalid id, producing a silent no-op the
    // user couldn't interpret. Pin: disabled must also gate Confirm
    // in confirming state.
    const onConfirm = vi.fn();
    const { rerender } = render(
      <ConfirmButton
        idleLabel="Unbind…"
        confirmLabel="Confirm unbind #42"
        onConfirm={onConfirm}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Unbind…' }));
    expect(screen.getByRole('button', { name: 'Confirm unbind #42' })).toBeEnabled();

    // Simulate parent re-render with input now empty → disabled=true.
    rerender(
      <ConfirmButton
        idleLabel="Unbind…"
        confirmLabel="Confirm unbind #null"
        onConfirm={onConfirm}
        disabled
      />,
    );
    expect(screen.getByRole('button', { name: 'Confirm unbind #null' })).toBeDisabled();

    // Defensive click — must not fire onConfirm.
    fireEvent.click(screen.getByRole('button', { name: 'Confirm unbind #null' }));
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('pending=true in confirming state disables Confirm AND Cancel', () => {
    // Belt against the synchronous-double-click race. If a rapid user
    // re-enters confirming after a first Confirm fired (idle button
    // not yet disabled by parent's pending prop), the SECOND Confirm
    // click MUST be no-op — otherwise non-idempotent mutations (rotate
    // secret) would fire twice.
    const onConfirm = vi.fn();
    // Render directly in confirming state isn't possible (state is
    // internal), so simulate: click idle to enter confirming, then
    // re-render with pending=true.
    const { rerender } = render(
      <ConfirmButton idleLabel="Rotate…" confirmLabel="Confirm" onConfirm={onConfirm} />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Rotate…' }));
    expect(screen.getByRole('button', { name: 'Confirm' })).toBeEnabled();

    rerender(
      <ConfirmButton
        idleLabel="Rotate…"
        confirmLabel="Confirm"
        onConfirm={onConfirm}
        pending
      />,
    );
    expect(screen.getByRole('button', { name: 'Confirm' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();

    // Click MUST be a no-op even if a programmatic click bypasses the
    // disabled prop somehow. Belt-and-braces.
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
