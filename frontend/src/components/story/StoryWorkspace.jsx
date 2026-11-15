import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import Button from '../ui/Button';
import EmptyState from '../ui/EmptyState';
import ConceptCards from './ConceptCards';
import IntelligencePanel from './IntelligencePanel';
import JobStrip from './JobStrip';
import RenderPanel from './RenderPanel';
import ScriptPicker from './ScriptPicker';
import ScriptsPanel from './ScriptsPanel';
import SourceSummary from './SourceSummary';
import StepRail from './StepRail';
import StoryboardEditor from './StoryboardEditor';
import VoicePanel from './VoicePanel';
import useAction from './useAction';
import { STEP_DEFS, approvedScripts, currentStep, deriveSteps } from './steps';
import { cancelJob, getProject } from '../../api/story';
import useJobPolling, { isTerminal } from '../../hooks/useJobPolling';

/**
 * One project, resumable: every step state is derived from GET /story/projects/{id}.
 * A running job is polled through GET /jobs/{id}; conflicting actions are disabled while it runs.
 */
export default function StoryWorkspace({ projectId }) {
  const [project, setProject] = useState(null);
  const [state, setState] = useState('loading');
  const [loadError, setLoadError] = useState('');
  const [job, setJob] = useState(null);
  const [step, setStep] = useState(null);
  const [scriptId, setScriptId] = useState('');
  const [pollError, setPollError] = useState('');
  const cancel = useAction();

  const reload = useCallback(async () => {
    const next = await getProject(projectId);
    setProject(next);
    setJob(next.active_job || null);
    return next;
  }, [projectId]);

  useEffect(() => {
    let current = true;
    setState('loading');
    setStep(null);
    getProject(projectId)
      .then(next => {
        if (!current) return;
        setProject(next);
        setJob(next.active_job || null);
        setStep(currentStep(deriveSteps(next)));
        setState('ready');
      })
      .catch(e => {
        if (!current) return;
        setLoadError(e.status === 404 ? 'This story project does not exist or was deleted.' : e.message);
        setState('error');
      });
    return () => { current = false; };
  }, [projectId]);

  const onTerminal = useCallback(async () => {
    try {
      await reload();
    } catch (e) {
      setPollError(e.message);
    }
  }, [reload]);
  useJobPolling(job?.id, job?.status, { onUpdate: setJob, onTerminal, onError: setPollError });

  const refresh = useCallback(() => reload().catch(e => setPollError(e.message)), [reload]);
  const startJob = useCallback(async make => {
    const started = await make();
    setPollError('');
    setJob(started);
    return started;
  }, []);

  if (state === 'loading') return <p className="muted" aria-live="polite">Loading project</p>;
  if (state === 'error') {
    return (
      <EmptyState title="This project could not be opened." action={<Link className="button secondary" to="/story">Back to projects</Link>}>
        {loadError}
      </EmptyState>
    );
  }

  const steps = deriveSteps(project);
  const active = step || 'input';
  const busyJob = Boolean(job && !isTerminal(job.status));
  const approved = approvedScripts(project);
  const narrated = approved.filter(s => s.narration);
  const pool = active === 'storyboard' ? narrated : approved;
  const script = pool.find(s => s.id === scriptId) || pool[0];
  const lastJob = (project.jobs || [])[0];
  const index = STEP_DEFS.findIndex(s => s.key === active);
  const next = STEP_DEFS.slice(index + 1).find(s => steps[s.key].state !== 'blocked');
  const common = { project, busyJob, onStart: startJob, onChanged: refresh };

  async function doCancel() {
    const done = await cancel.run(() => cancelJob(job.id));
    if (done) await refresh();
  }

  return (
    <div className="story-workspace">
      <div className="story-bar">
        <Link className="button ghost small" to="/story">All projects</Link>
        <h2 className="story-project-title">{project.title}</h2>
      </div>
      <StepRail steps={steps} active={active} onSelect={setStep} />
      <JobStrip job={busyJob ? job : null} onCancel={doCancel} cancelling={cancel.busy} cancelError={cancel.error} />
      <div aria-live="polite">
        {pollError && <p className="story-error" role="alert">{pollError}</p>}
        {!busyJob && lastJob?.status === 'failed' && (
          <p className="story-error" role="alert">The last step failed: {lastJob.error_message || lastJob.error_code}</p>
        )}
      </div>

      {active === 'input' && <SourceSummary project={project} />}
      {active === 'intelligence' && <IntelligencePanel {...common} />}
      {active === 'concepts' && <ConceptCards {...common} />}
      {active === 'scripts' && <ScriptsPanel {...common} />}
      {(active === 'narration' || active === 'storyboard') && (
        <>
          <ScriptPicker scripts={pool} value={script?.id || ''} onChange={setScriptId} />
          {script && active === 'narration' && <VoicePanel key={script.id} project={project} script={script} busyJob={busyJob} onChanged={refresh} />}
          {script && active === 'storyboard' && <StoryboardEditor key={script.id} project={project} script={script} busyJob={busyJob} onChanged={refresh} />}
        </>
      )}
      {active === 'render' && <RenderPanel {...common} />}

      {steps[active].state === 'complete' && next && (
        <div className="form-actions">
          <Button variant="primary" onClick={() => setStep(next.key)}>Continue to {next.label}</Button>
        </div>
      )}
    </div>
  );
}
