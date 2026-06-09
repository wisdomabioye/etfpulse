/**
 * Render + null-handling tests for the R2 signal-context components. The key
 * coverage is the NULL paths the prototype lacked — AI-failed signals carry
 * null action/confidence/confirmation/headline and must degrade to "—" /
 * "AI analysis pending" without crashing.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { SignalListItem } from '../../api/types';
import { LivePulse, SignalCardMini, SignalRow } from './index';

const signal = (over: Partial<SignalListItem> = {}): SignalListItem => ({
  id: 42,
  asset: 'BTC',
  signal_type: 'flow_anomaly',
  status: 'alerted',
  confidence: 8,
  headline: 'BTC inflow streak breaks after 5 days',
  suggested_action: 'consider long',
  time_horizon: 'swing',
  signal_date: '2026-06-08',
  created_at: new Date().toISOString(),
  expires_at: null,
  alerted_to: 3,
  confirmation_score: 2,
  ...over,
});

describe('SignalRow', () => {
  it('renders the headline, action, confidence, and confirmation', () => {
    render(<SignalRow signal={signal()} />);
    expect(screen.getByText('BTC inflow streak breaks after 5 days')).toBeInTheDocument();
    expect(screen.getByText('Long')).toBeInTheDocument();
    expect(screen.getByText('8')).toBeInTheDocument();
    expect(screen.getByText('conf 2/3')).toBeInTheDocument();
  });

  it('fires onClick on click and on Enter', () => {
    const onClick = vi.fn();
    render(<SignalRow signal={signal()} onClick={onClick} />);
    const row = screen.getByRole('button');
    fireEvent.click(row);
    fireEvent.keyDown(row, { key: 'Enter' });
    expect(onClick).toHaveBeenCalledTimes(2);
  });

  it('degrades gracefully for an AI-failed signal (all-null)', () => {
    render(
      <SignalRow
        signal={signal({
          headline: null,
          suggested_action: null,
          confidence: null,
          confirmation_score: null,
          time_horizon: null,
        })}
      />,
    );
    expect(screen.getByText('AI analysis pending')).toBeInTheDocument();
    // No crash; multiple "—" placeholders rendered.
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText('Long')).not.toBeInTheDocument();
  });
});

describe('SignalCardMini', () => {
  it('renders headline + action + confidence', () => {
    render(<SignalCardMini signal={signal()} />);
    expect(screen.getByText('BTC inflow streak breaks after 5 days')).toBeInTheDocument();
    expect(screen.getByText('Long')).toBeInTheDocument();
    expect(screen.getByText('8')).toBeInTheDocument();
  });
  it('handles a null-AI signal', () => {
    render(<SignalCardMini signal={signal({ headline: null, suggested_action: null, confidence: null })} />);
    expect(screen.getByText('AI analysis pending')).toBeInTheDocument();
  });
});

describe('LivePulse', () => {
  it('renders up to `limit` items and fires onPick', () => {
    const onPick = vi.fn();
    const signals = [signal({ id: 1 }), signal({ id: 2 }), signal({ id: 3 })];
    render(<LivePulse signals={signals} limit={2} onPick={onPick} />);
    const rows = screen.getAllByRole('button');
    expect(rows).toHaveLength(2); // limited
    fireEvent.click(rows[0]);
    expect(onPick).toHaveBeenCalledWith(signals[0]);
  });
  it('fires onPick on keyboard activation', () => {
    const onPick = vi.fn();
    const signals = [signal({ id: 7 })];
    render(<LivePulse signals={signals} onPick={onPick} />);
    fireEvent.keyDown(screen.getByRole('button'), { key: ' ' });
    expect(onPick).toHaveBeenCalledWith(signals[0]);
  });
  it('renders nothing for an empty stream', () => {
    render(<LivePulse signals={[]} />);
    expect(screen.queryAllByRole('button')).toHaveLength(0);
  });
});
