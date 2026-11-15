import { STEP_DEFS } from './steps';

const STATE_TEXT = { complete: 'Complete', available: 'Available', blocked: 'Blocked' };

/** Step rail: complete / available / blocked, with the reason a blocked step cannot open yet. */
export default function StepRail({ steps, active, onSelect }) {
  return (
    <nav className="step-rail" aria-label="Story steps">
      <ol>
        {STEP_DEFS.map((step, index) => {
          const info = steps[step.key];
          const blocked = info.state === 'blocked';
          return (
            <li key={step.key} className={`step ${info.state}${active === step.key ? ' current' : ''}`}>
              <button
                type="button"
                disabled={blocked}
                aria-current={active === step.key ? 'step' : undefined}
                aria-describedby={blocked ? `step-${step.key}-why` : undefined}
                onClick={() => onSelect(step.key)}
              >
                <span className="step-index" aria-hidden="true">{info.state === 'complete' ? '✓' : index + 1}</span>
                <span className="step-text">
                  <strong>{step.label}</strong>
                  <small>{STATE_TEXT[info.state]}</small>
                </span>
              </button>
              {blocked && <small id={`step-${step.key}-why`} className="step-why">{info.reason}</small>}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
