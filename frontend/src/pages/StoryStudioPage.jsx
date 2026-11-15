import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import ProjectList from '../components/story/ProjectList';
import StoryInputForm from '../components/story/StoryInputForm';
import StoryWorkspace from '../components/story/StoryWorkspace';
import { listProjects } from '../api/story';
import PageHeading from '../layout/PageHeading';

function ProjectsView() {
  const navigate = useNavigate();
  const [projects, setProjects] = useState([]);
  const [state, setState] = useState('loading');
  const [error, setError] = useState('');
  const [creating, setCreating] = useState(false);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let current = true;
    setState('loading');
    listProjects()
      .then(data => {
        if (!current) return;
        setProjects(data?.projects || []);
        setState('ready');
      })
      .catch(e => {
        if (!current) return;
        setError(e.message);
        setState('error');
      });
    return () => { current = false; };
  }, [reload]);

  const refresh = useCallback(() => setReload(n => n + 1), []);

  if (creating) {
    return <StoryInputForm onCreated={project => navigate(`/story/${project.id}`)} onCancel={() => setCreating(false)} />;
  }
  return <ProjectList projects={projects} state={state} error={error} onRetry={refresh} onNew={() => setCreating(true)} onChanged={refresh} />;
}

/**
 * AI Story Studio (routes `/story` and `/story/:projectId`): a guided, resumable
 * workflow from source to rendered videos. All state comes from the API.
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
      {projectId ? <StoryWorkspace key={projectId} projectId={projectId} /> : <ProjectsView />}
    </>
  );
}
