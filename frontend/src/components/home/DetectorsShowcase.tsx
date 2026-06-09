import { SIGNAL_TYPES, DETECTORS } from '../../lib/constants';
import { detectorColorToken } from '../../lib/colors';
import { cssVar } from '../../lib/colorMix';
import { DetectorIcon } from '../ui';

/**
 * Five-detector showcase grid — each cell carries the detector's icon, name,
 * and one-line "what it catches", topped by a 2px rule in the detector's
 * identity color. Ported from the prototype's Home detector section; content
 * comes from the canonical `DETECTORS` config (no duplication).
 */
export function DetectorsShowcase() {
  return (
    <div className="grid grid-cols-1 xs:grid-cols-2 md:grid-cols-5 gap-3">
      {SIGNAL_TYPES.map((type) => (
        <div
          key={type}
          className="py-[18px] px-4 bg-bg-2 border border-line-2 rounded-md"
          style={{ borderTop: `2px solid ${cssVar(detectorColorToken(type))}` }}
        >
          <DetectorIcon type={type} size={20} />
          <div className="text-[14px] font-semibold mt-3 mb-1.5">{DETECTORS[type].label}</div>
          <div className="text-[11.5px] text-t3 leading-[1.5]">{DETECTORS[type].catches}</div>
        </div>
      ))}
    </div>
  );
}
