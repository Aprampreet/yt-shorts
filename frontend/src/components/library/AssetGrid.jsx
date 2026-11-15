import AssetCard from './AssetCard';

export default function AssetGrid({ items, onOpen }) {
  return (
    <ul className="asset-grid" aria-label="Assets">
      {items.map(item => <AssetCard key={item.id} asset={item} onOpen={onOpen} />)}
    </ul>
  );
}
