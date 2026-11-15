import { useState } from 'react';
import Button from '../ui/Button';
import Field from '../ui/Field';
import ErrorNote from './ErrorNote';
import useAction from './useAction';
import { CARD_FIELDS, TRANSITIONS, VISUAL_TYPES } from './constants';
import { buildCard, cardProblem, cardValues, isCardType } from './cardForm';
import { editScene } from '../../api/story';

const ASSET_TYPES = ['asset_video', 'asset_image', 'source_clip'];

function eligibleAssets(visualType, assets, current) {
  const list = assets.filter(a => {
    if (a.license_status === 'restricted' || a.deleted_at) return false;
    if (visualType === 'asset_video') return a.kind === 'video' && a.origin !== 'source_ingest';
    if (visualType === 'asset_image') return a.kind === 'image' || a.kind === 'graphic';
    if (visualType === 'source_clip') return a.kind === 'video' && a.origin === 'source_ingest';
    return false;
  });
  if (current && !list.some(a => a.id === current.id)) list.unshift(current);
  return list;
}

/** Edits one scene: visual type, library asset, card fields (with the card_specs limits) and transition. */
export default function SceneEditor({ scene, assets, onSaved, onCancel }) {
  const [visualType, setVisualType] = useState(scene.visual_type);
  const [transition, setTransition] = useState(scene.transition);
  const [text, setText] = useState(scene.narration_text);
  const [assetId, setAssetId] = useState(scene.asset?.id || '');
  const [claimId, setClaimId] = useState(scene.claim_id || '');
  const [values, setValues] = useState(() => cardValues(scene.visual_type, scene.card));
  const [invalid, setInvalid] = useState('');
  const { busy, error, run } = useAction();

  const isCard = isCardType(visualType);
  const needsAsset = ASSET_TYPES.includes(visualType);
  const options = eligibleAssets(visualType, assets, scene.asset);

  function changeType(next) {
    setVisualType(next);
    setValues(cardValues(next, scene.card));
    if (next !== scene.visual_type && !ASSET_TYPES.includes(next)) setAssetId('');
    if (!eligibleAssets(next, assets, scene.asset).some(a => a.id === assetId)) setAssetId('');
  }

  async function save(event) {
    event.preventDefault();
    const problem = isCard ? cardProblem(visualType, values) : needsAsset && !assetId ? 'Choose a library asset for this visual.' : '';
    setInvalid(problem);
    if (problem) return;
    const changes = {};
    const typeChanged = visualType !== scene.visual_type;
    if (text !== scene.narration_text) changes.narration_text = text;
    if (typeChanged) changes.visual_type = visualType;
    if (transition !== scene.transition) changes.transition = transition;
    if (needsAsset && (typeChanged || assetId !== (scene.asset?.id || ''))) changes.asset_id = assetId;
    if (visualType === 'source_clip' && claimId !== (scene.claim_id || '')) changes.claim_id = claimId;
    if (isCard) {
      const card = buildCard(visualType, values, scene.card);
      if (typeChanged || JSON.stringify(card) !== JSON.stringify(scene.card)) changes.card = card;
    }
    if (!Object.keys(changes).length) {
      setInvalid('Nothing changed.');
      return;
    }
    const saved = await run(() => editScene(scene.id, changes));
    if (saved) onSaved(saved);
  }

  return (
    <form className="scene-editor" onSubmit={save} aria-label={`Edit scene ${scene.order}`}>
      <div className="form-grid">
        <Field label="Visual type">
          <select value={visualType} onChange={e => changeType(e.target.value)}>
            {VISUAL_TYPES.map(t => <option key={t.value} value={t.value}>{t.label}</option>)}
          </select>
        </Field>
        <Field label="Transition">
          <select value={transition} onChange={e => setTransition(e.target.value)}>
            {TRANSITIONS.map(t => <option key={t.value} value={t.value}>{t.label}</option>)}
          </select>
        </Field>
      </div>
      <Field label="Narration text for this scene">
        <textarea rows={2} maxLength={2000} value={text} onChange={e => setText(e.target.value)} />
      </Field>
      {needsAsset && (
        <Field label="Library asset" hint={options.length ? '' : 'No eligible asset in your library for this visual type.'}>
          <select value={assetId} onChange={e => setAssetId(e.target.value)}>
            <option value="">Choose an asset</option>
            {options.map(a => <option key={a.id} value={a.id}>{a.title} · {a.license_label}</option>)}
          </select>
        </Field>
      )}
      {visualType === 'source_clip' && (
        <Field label="Claim id" hint="Source clips must be tied to a claim, for example c3. Third-party footage is limited to a share of the video.">
          <input value={claimId} onChange={e => setClaimId(e.target.value)} />
        </Field>
      )}
      {isCard && CARD_FIELDS[visualType].map(field => {
        const value = values[field.key] || '';
        const set = next => setValues(old => ({ ...old, [field.key]: next }));
        const hint = field.list ? `One per line, up to ${field.maxItems} items of ${field.max} characters` : `${value.length} / ${field.max}`;
        return (
          <Field key={field.key} label={field.label + (field.required ? '' : ' (optional)')} hint={hint}>
            {field.multiline || field.list
              ? <textarea rows={field.list ? 5 : 3} value={value} onChange={e => set(e.target.value)} />
              : <input value={value} onChange={e => set(e.target.value)} />}
          </Field>
        );
      })}
      {invalid && <p className="field-error" role="alert">{invalid}</p>}
      <ErrorNote error={error} />
      <div className="form-actions">
        <Button type="submit" variant="primary" busy={busy}>Save scene</Button>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
    </form>
  );
}
