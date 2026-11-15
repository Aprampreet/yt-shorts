import { Link, useParams } from 'react-router-dom';
import PageHeading from '../layout/PageHeading';

/**
 * AI Story Studio (routes `/story` and `/story/:projectId`).
 * Honest shell: the story pipeline is not part of this build yet, so the page
 * shows no projects, no previews and no progress.
 */
export default function StoryStudioPage({ notices }) {
  const { projectId } = useParams();

  return (
    <>
      <PageHeading
        eyebrow="AI STORY STUDIO"
        title="From an idea"
        subtitle="to a narrated story."
        description="Research, script, voice and storyboard original vertical stories."
      />
      {notices}

      <section className="empty-state" aria-labelledby="story-empty-title">
        <h2 id="story-empty-title">Story Studio is not available yet in this build.</h2>
        {projectId ? (
          <p>
            Story project <code>{projectId}</code> cannot be opened here until Story Studio ships. Nothing has been
            created or changed.
          </p>
        ) : (
          <p>There are no story projects to show. This page will list them once Story Studio is available.</p>
        )}
        <Link className="button secondary" to="/">Create Shorts instead</Link>
      </section>
    </>
  );
}
