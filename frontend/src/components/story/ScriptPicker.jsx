import { scriptTitle } from './constants';

/** Chooses which approved script a step works on. Hidden when there is only one. */
export default function ScriptPicker({ scripts, value, onChange, label = 'Script' }) {
  if (scripts.length < 2) return null;
  return (
    <div className="field script-picker">
      <label htmlFor="story-script-picker">{label}</label>
      <select id="story-script-picker" value={value} onChange={e => onChange(e.target.value)}>
        {scripts.map((script, index) => <option key={script.id} value={script.id}>{scriptTitle(script, index)}</option>)}
      </select>
    </div>
  );
}
