import Scheduler from '../components/scheduler';
import PageHeading from '../layout/PageHeading';

/**
 * Publishing Studio (route `/publish`). Currently wraps the existing YouTube
 * Scheduler unchanged. The legacy OAuth callback URL
 * `/?view=schedule&connected=…` is redirected here with its query intact.
 */
export default function PublishingPage({ notices, initialFilter = 'calendar' }) {
  return (
    <>
      <PageHeading
        title="Your YouTube queue."
        subtitle="On your schedule."
        description="Connect YouTube once and track each upload here."
      />
      {notices}
      <Scheduler initialFilter={initialFilter} />
    </>
  );
}
