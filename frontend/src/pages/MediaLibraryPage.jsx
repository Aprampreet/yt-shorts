import { useCallback, useEffect, useState } from 'react';
import { listAssets } from '../api/assets';
import AssetDrawer from '../components/library/AssetDrawer';
import AssetGrid from '../components/library/AssetGrid';
import LibraryFilters from '../components/library/LibraryFilters';
import UploadPanel from '../components/library/UploadPanel';
import Button from '../components/ui/Button';
import EmptyState from '../components/ui/EmptyState';
import Spinner from '../components/ui/Spinner';
import PageHeading from '../layout/PageHeading';

const PAGE_SIZE = 24;
const NO_FILTERS = { q: '', kind: '', license_status: '', tag: '' };

/**
 * Media Library (route `/library`): owner-scoped assets with their licence
 * and attribution, backed by GET/POST/PATCH/DELETE /v1/assets.
 */
export default function MediaLibraryPage({ notices }) {
  const [filters, setFilters] = useState(NO_FILTERS);
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState({ items: [], total: 0 });
  const [state, setState] = useState('loading');
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const [uploading, setUploading] = useState(false);
  const [selected, setSelected] = useState(null);

  useEffect(() => {
    let current = true;
    setState('loading');
    listAssets({ ...filters, limit: PAGE_SIZE, offset })
      .then(data => {
        if (!current) return;
        setPage({ items: data?.items || [], total: data?.total || 0 });
        setState('ready');
      })
      .catch(e => {
        if (!current) return;
        setError(e.message);
        setState('error');
      });
    return () => { current = false; };
  }, [filters, offset, reload]);

  const changeFilters = useCallback(patch => {
    setFilters(old => ({ ...old, ...patch }));
    setOffset(0);
  }, []);
  const closeDrawer = useCallback(() => setSelected(null), []);
  const refresh = useCallback(() => setReload(n => n + 1), []);

  function replaceItem(updated) {
    setPage(old => ({ ...old, items: old.items.map(item => (item.id === updated.id ? updated : item)) }));
    setSelected(updated);
  }

  function removeItem(removed) {
    setSelected(null);
    setReload(n => n + 1);
    setPage(old => ({ items: old.items.filter(item => item.id !== removed.id), total: Math.max(0, old.total - 1) }));
  }

  const hasFilters = Object.values(filters).some(Boolean);
  const first = page.total ? offset + 1 : 0;
  const last = Math.min(offset + PAGE_SIZE, page.total);

  return (
    <>
      <PageHeading
        eyebrow="MEDIA LIBRARY"
        title="Your licensed assets."
        subtitle="In one place."
        description="Footage, images, music and voice with their license and attribution."
      />
      {notices}

      <section className="library-toolbar" aria-label="Library actions">
        <LibraryFilters filters={filters} onChange={changeFilters} />
        <Button
          variant={uploading ? 'secondary' : 'primary'}
          aria-expanded={uploading}
          aria-controls="upload-region"
          onClick={() => setUploading(open => !open)}
        >
          {uploading ? 'Hide upload' : 'Upload asset'}
        </Button>
      </section>

      {uploading && (
        <section id="upload-region" className="panel" aria-labelledby="upload-title">
          <h2 id="upload-title" className="panel-title">Add to library</h2>
          <UploadPanel onUploaded={refresh} />
        </section>
      )}

      <section aria-label="Asset results" aria-busy={state === 'loading'}>
        <p className="results-count" role="status" aria-live="polite">
          {state === 'loading' && <Spinner label="Loading assets" />}
          {state === 'ready' && (page.total ? `Showing ${first}–${last} of ${page.total}` : '')}
        </p>

        {state === 'error' && (
          <div className="error-banner" role="alert">
            Could not load the library: {error}
            <Button size="sm" variant="ghost" onClick={refresh}>Retry</Button>
          </div>
        )}

        {state === 'ready' && page.items.length === 0 && (
          <EmptyState title={hasFilters ? 'No assets match these filters.' : 'Your library is empty.'}>
            {hasFilters
              ? 'Try a different search, or clear the filters.'
              : 'Upload footage, images or audio together with their licence so every project can use them safely.'}
          </EmptyState>
        )}

        {page.items.length > 0 && <AssetGrid items={page.items} onOpen={setSelected} />}

        {page.total > PAGE_SIZE && (
          <nav className="pagination" aria-label="Library pages">
            <Button size="sm" disabled={offset === 0 || state === 'loading'} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
              Previous
            </Button>
            <Button size="sm" disabled={offset + PAGE_SIZE >= page.total || state === 'loading'} onClick={() => setOffset(offset + PAGE_SIZE)}>
              Next
            </Button>
          </nav>
        )}
      </section>

      {selected && (
        <AssetDrawer
          key={selected.id}
          asset={selected}
          onClose={closeDrawer}
          onChanged={replaceItem}
          onDeleted={removeItem}
          onTagClick={tag => { setSelected(null); changeFilters({ tag }); }}
        />
      )}
    </>
  );
}
