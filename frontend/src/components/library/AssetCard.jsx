import { asset as assetUrl } from '../api';
import Badge from '../ui/Badge';
import LicenseBadge from './LicenseBadge';
import { formatDuration, mediaClass } from './licenses';

const GLYPHS = { audio: '♪', other: '▤', video: '▶', image: '▣' };

/** One tile in the asset grid: thumbnail (video/image) or a type glyph, title and licence. */
export default function AssetCard({ asset, onOpen }) {
  const cls = mediaClass(asset.kind);
  const duration = formatDuration(asset.duration);
  return (
    <li>
      <button type="button" className="asset-card" onClick={() => onOpen(asset)} aria-label={`Open ${asset.title || 'untitled asset'}`}>
        <span className={`asset-thumb ${cls}`}>
          {asset.thumbnail_url ? (
            <img src={assetUrl(asset.thumbnail_url)} alt="" loading="lazy" />
          ) : (
            <span className="asset-glyph" aria-hidden="true">{GLYPHS[cls]}</span>
          )}
          <span className="asset-kind">{asset.kind}</span>
          {duration && <span className="asset-duration">{duration}</span>}
          {asset.pinned && <span className="asset-pin">Pinned</span>}
        </span>
        <span className="asset-meta">
          <strong>{asset.title || 'Untitled'}</strong>
          <LicenseBadge asset={asset} />
          {asset.usage?.projects > 0 && (
            <Badge>Used in {asset.usage.projects} {asset.usage.projects === 1 ? 'project' : 'projects'}</Badge>
          )}
        </span>
      </button>
    </li>
  );
}
