import { useCalibration } from '../../api/queries';
import { calibrationCells } from '../../lib/calibrationCells';
import { CalibrationCurve } from '../charts';
import { Button, Card, Kicker, Skeleton } from '../ui';

/**
 * Home calibration teaser — the signature reliability curve in a split card,
 * with the "does confidence 8 actually win 80%?" pitch and a CTA to the full
 * Proof surface. Reads the real `useCalibration` cohort and maps the swing
 * horizon to chart cells. Ported from the prototype's Home proof teaser.
 */
export function CalibrationTeaser() {
  const { data, isLoading } = useCalibration();
  const cells = data ? calibrationCells(data, 'swing') : [];

  return (
    <Card pad={false}>
      <div className="grid grid-cols-1 md:grid-cols-[1fr_1.1fr]">
        <div className="p-8 md:border-r border-line-2">
          <Kicker>Signature proof</Kicker>
          <h3 className="text-[24px] font-semibold tracking-[-0.02em] mt-3 mb-3">
            Does confidence 8 actually win 80%?
          </h3>
          <p className="text-t2 text-[14px] leading-[1.6] mb-[22px]">
            Our calibration curve plots observed hit-rate against the ideal diagonal, with Wilson
            95% confidence intervals and honest sample counts. Where we&apos;re miscalibrated, the
            dots turn red.
          </p>
          <Button as="link" to="/track-record" variant="primary" size="md">
            Open the proof surface →
          </Button>
        </div>
        <div className="py-6 px-7 bg-bg-1">
          {isLoading ? (
            <Skeleton className="h-[260px] w-full" />
          ) : cells.length ? (
            <CalibrationCurve cells={cells} horizon="swing" height={260} />
          ) : (
            <div className="h-[260px] flex items-center justify-center text-t3 text-[13px] text-center">
              Not enough scored signals yet to plot a reliability curve.
            </div>
          )}
        </div>
      </div>
    </Card>
  );
}
