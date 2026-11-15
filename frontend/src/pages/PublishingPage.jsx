import { useCallback, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import CreatePostPanel from '../components/publishing/CreatePostPanel';
import PlatformsPanel from '../components/publishing/PlatformsPanel';
import Scheduler from '../components/scheduler';
import PageHeading from '../layout/PageHeading';

const CONNECTED_LABELS = { youtube: 'YouTube', instagram: 'Instagram' };

/**
 * Publishing Studio (route `/publish`): platform status, create-a-post flow
 * and the queue. The OAuth callbacks redirect to `/?view=schedule&connected=…`,
 * which the router forwards here with `connected` intact.
 */
export default function PublishingPage({ notices, initialFilter = 'calendar' }) {
  const [params] = useSearchParams();
  const [platforms, setPlatforms] = useState(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const bump = useCallback(() => setRefreshKey(n => n + 1), []);
  const connected = CONNECTED_LABELS[params.get('connected')];

  return (
    <>
      <PageHeading
        title="Your publishing queue."
        subtitle="On your schedule."
        description="Connect your channels, review each post's metadata and track every upload here."
      />
      {notices}
      {connected && <div className="notice" role="status">{connected} connected</div>}

      <PlatformsPanel onLoaded={setPlatforms} />
      <CreatePostPanel platforms={platforms} onChanged={bump} />
      <Scheduler
        initialFilter={initialFilter}
        refreshKey={refreshKey}
        instagramConfigured={platforms?.instagram?.configured === true}
        onPostsChanged={bump}
      />
    </>
  );
}
