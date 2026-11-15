import Badge from '../ui/Badge';
import LicenseBadge from '../library/LicenseBadge';
import { LICENSE_OPTIONS } from '../library/licenses';

const REVIEW_STATUSES = ['unknown', 'permission_pending', 'restricted'];

function licenseOf(entry) {
  const label = LICENSE_OPTIONS.find(o => o.value === entry.license_status)?.label || entry.license_status;
  return { license_status: entry.license_status, license_label: label, review_required: REVIEW_STATUSES.includes(entry.license_status) };
}

/**
 * Rights review for one rendered video: every asset used with its licence, attribution,
 * the synthetic-voice disclosure and the third-party footage flag. It reports what the
 * project used; it is not a legal clearance.
 */
export default function RightsReport({ video }) {
  const report = video.quality_report || {};
  const rights = report.rights_review || {};
  const provenance = report.provenance || [];
  const narration = report.narration || {};
  const synthetic = Boolean(narration.synthetic_voice ?? rights.synthetic_voice);
  const thirdParty = (rights.third_party_source_footage || []).length > 0 || provenance.some(p => p.third_party_source_footage);
  const needsReview = rights.status === 'manual_review_required';

  return (
    <section className="rights-report" aria-label={`Rights review for ${video.title}`}>
      <div className="badge-row">
        <strong>Rights review</strong>
        <Badge tone={needsReview ? 'warn' : 'success'}>{needsReview ? 'Manual review required' : 'Rights documented'}</Badge>
        {thirdParty && <Badge tone="warn">Third-party footage</Badge>}
        {synthetic && <Badge tone="accent">Synthetic voice</Badge>}
      </div>
      {synthetic && (
        <p className="muted">{narration.disclosure || 'This video is narrated with a synthetic (AI-generated) voice.'}</p>
      )}
      {thirdParty && (
        <p className="muted">
          This video includes third-party source footage. It stays covered only by the rights declaration you made for the source.
        </p>
      )}
      {provenance.length > 0 ? (
        <ul className="provenance" aria-label="Assets used">
          {provenance.map(entry => (
            <li key={entry.asset_id}>
              <span>{entry.title || entry.asset_id} <span className="muted">({entry.used_as || entry.kind})</span></span>
              <LicenseBadge asset={licenseOf(entry)} />
              {entry.attribution_text && <span className="muted">Credit: {entry.attribution_text}</span>}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">All visuals in this video are original graphics.</p>
      )}
      {(rights.attribution || []).length > 0 && (
        <p className="muted">Credits added to the description: {[...new Set(rights.attribution)].join('; ')}</p>
      )}
      {rights.copyright_note && <p className="muted">{rights.copyright_note}</p>}
    </section>
  );
}
