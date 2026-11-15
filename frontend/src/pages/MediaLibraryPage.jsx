import { Link } from 'react-router-dom';
import PageHeading from '../layout/PageHeading';

/**
 * Media Library (route `/library`).
 * Honest shell: asset storage is not part of this build yet, so no assets
 * are listed.
 */
export default function MediaLibraryPage({ notices }) {
  return (
    <>
      <PageHeading
        eyebrow="MEDIA LIBRARY"
        title="Your licensed assets."
        subtitle="In one place."
        description="Footage, images, music and voice with their license and attribution."
      />
      {notices}

      <section className="empty-state" aria-labelledby="library-empty-title">
        <h2 id="library-empty-title">The Media Library is not available yet in this build.</h2>
        <p>No assets are stored here yet. Source videos you upload for Shorts stay attached to their projects.</p>
        <Link className="button secondary" to="/history">Open recent projects</Link>
      </section>
    </>
  );
}
