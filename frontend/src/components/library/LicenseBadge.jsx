import Badge from '../ui/Badge';

const TONES = {
  owned: 'success',
  commercial_use_permitted: 'success',
  attribution_required: 'accent',
  permission_pending: 'warn',
  unknown: 'warn',
  restricted: 'danger',
};

/** Licence label from the API, with "review required" when the backend says so. */
export default function LicenseBadge({ asset }) {
  const label = asset.license_label || asset.license_status;
  return (
    <Badge tone={TONES[asset.license_status] || 'neutral'}>
      {label}
      {asset.review_required && ' · review required'}
    </Badge>
  );
}
