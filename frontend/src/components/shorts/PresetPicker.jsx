import { PRESETS } from './options';

/** Radio group of edit presets with a description under each. */
export default function PresetPicker({ value, onChange, disabled }) {
  return (
    <fieldset className="preset-picker" disabled={disabled}>
      <legend>Edit preset</legend>
      <div className="preset-options">
        {PRESETS.map(preset => (
          <label key={preset.value} className={`preset-option ${value === preset.value ? 'selected' : ''}`}>
            <input
              type="radio"
              name="edit-preset"
              value={preset.value}
              checked={value === preset.value}
              onChange={() => onChange(preset.value)}
            />
            <span>
              <strong>{preset.label}</strong>
              <small>{preset.help}</small>
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}
