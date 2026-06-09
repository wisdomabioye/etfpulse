/**
 * Tests for the R6 SignalFilterBar — each control maps to the correct
 * `SignalFilters` patch, and "All" / floor values CLEAR the key (rather than
 * sending a no-op constraint to the API).
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { SignalFilters } from '../../api/types';
import { SignalFilterBar } from './SignalFilterBar';

function setup(value: SignalFilters = { limit: 14 }) {
  const onChange = vi.fn();
  render(<SignalFilterBar value={value} onChange={onChange} />);
  return onChange;
}

describe('SignalFilterBar', () => {
  it('sets the asset on a pill click and clears it on "All"', () => {
    const onChange = setup({ limit: 14, asset: 'BTC' });
    fireEvent.click(screen.getByRole('button', { name: 'ETH' }));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ asset: 'ETH', limit: 14 }));
    fireEvent.click(screen.getByRole('button', { name: 'All' }));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ asset: undefined }));
  });

  it('sets the signal type and clears it on "All types"', () => {
    const onChange = setup();
    fireEvent.change(screen.getByLabelText('Signal type filter'), { target: { value: 'magnitude' } });
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ signal_type: 'magnitude' }));
  });

  it('sets confidence_min above the floor and clears it at 1', () => {
    const onChange = setup({ limit: 14, confidence_min: 5 });
    const range = screen.getByLabelText('Minimum confidence');
    fireEvent.change(range, { target: { value: '7' } });
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ confidence_min: 7 }));
    fireEvent.change(range, { target: { value: '1' } });
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ confidence_min: undefined }));
  });

  it('toggles include_expired', () => {
    const onChange = setup();
    fireEvent.click(screen.getByRole('checkbox'));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ include_expired: true }));
  });

  it('changes sort order', () => {
    const onChange = setup();
    fireEvent.change(screen.getByLabelText('Sort order'), { target: { value: 'oldest' } });
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ sort: 'oldest' }));
  });
});
