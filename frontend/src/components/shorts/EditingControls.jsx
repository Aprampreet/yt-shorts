import PresetPicker from './PresetPicker';
import SecondaryAssetPicker from './SecondaryAssetPicker';
import {
  CAPTION_PRESETS, CATEGORIES, COMPOSITIONS, COMPOSITION_HELP, CORNERS, usesCorner, usesSecondVideo,
} from './options';

/**
 * The 2.0 editing controls inside "Editing preferences". Values are the
 * exact API fields; `onChange(key, value)` updates one of them.
 */
export default function EditingControls({ options, onChange, disabled }) {
  return (
    <div className="editing-controls">
      <PresetPicker value={options.preset} onChange={value => onChange('preset', value)} disabled={disabled} />
      <div className="options-grid">
        <label>
          Composition
          <select value={options.composition} onChange={e => onChange('composition', e.target.value)} disabled={disabled}>
            {COMPOSITIONS.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
          {COMPOSITION_HELP[options.composition] && <small className="field-hint">{COMPOSITION_HELP[options.composition]}</small>}
        </label>
        <label>
          Caption preset
          <select value={options.caption_preset} onChange={e => onChange('caption_preset', e.target.value)} disabled={disabled}>
            {CAPTION_PRESETS.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
        </label>
        <label>
          Content category
          <select value={options.content_category} onChange={e => onChange('content_category', e.target.value)} disabled={disabled}>
            {CATEGORIES.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
        </label>
        {usesCorner(options) && (
          <label>
            Facecam corner
            <select value={options.facecam_corner} onChange={e => onChange('facecam_corner', e.target.value)} disabled={disabled}>
              {CORNERS.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
            </select>
          </label>
        )}
        {usesSecondVideo(options) && (
          <SecondaryAssetPicker
            value={options.secondary_asset_id}
            onChange={value => onChange('secondary_asset_id', value)}
            disabled={disabled}
          />
        )}
        <label className="wide rights-checkbox toggle">
          <input type="checkbox" checked={options.hook_overlay} onChange={e => onChange('hook_overlay', e.target.checked)} disabled={disabled} />
          Add a hook line at the start
        </label>
        <label className="wide rights-checkbox toggle">
          <input type="checkbox" checked={options.remove_dead_air} onChange={e => onChange('remove_dead_air', e.target.checked)} disabled={disabled} />
          Remove dead air (silent gaps)
        </label>
        <label className="wide rights-checkbox toggle">
          <input type="checkbox" checked={options.punch_ins} onChange={e => onChange('punch_ins', e.target.checked)} disabled={disabled} />
          Punch-in zooms on emphasis
        </label>
      </div>
    </div>
  );
}
