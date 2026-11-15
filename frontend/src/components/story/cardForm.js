import { CARD_FIELDS } from './constants';

export const isCardType = type => Object.hasOwn(CARD_FIELDS, type);

const getPath = (object, path) => path.split('.').reduce((value, key) => (value == null ? undefined : value[key]), object);

function setPath(object, path, value) {
  const keys = path.split('.');
  let cursor = object;
  keys.slice(0, -1).forEach(key => {
    cursor[key] = cursor[key] || {};
    cursor = cursor[key];
  });
  cursor[keys[keys.length - 1]] = value;
}

/** Form values (strings) for a card kind, taken from an existing card of the same kind. */
export function cardValues(kind, card) {
  const values = {};
  if (!isCardType(kind)) return values;
  CARD_FIELDS[kind].forEach(field => {
    const stored = card && card.kind === kind ? getPath(card, field.key) : undefined;
    values[field.key] = field.list ? (stored || []).join('\n') : stored || '';
  });
  return values;
}

/** Card payload for the API. Optional fields that are empty are left out; the kind is always sent. */
export function buildCard(kind, values, base) {
  const card = base && base.kind === kind ? structuredClone(base) : { kind };
  card.kind = kind;
  CARD_FIELDS[kind].forEach(field => {
    const raw = values[field.key] || '';
    if (field.list) {
      setPath(card, field.key, raw.split('\n').map(line => line.trim()).filter(Boolean));
    } else if (raw.trim() || field.required) {
      setPath(card, field.key, raw.trim());
    } else if (field.key.indexOf('.') === -1) {
      delete card[field.key];
    }
  });
  return card;
}

/** Client-side check against the card_specs limits. Returns a message or ''. The backend remains the authority. */
export function cardProblem(kind, values) {
  for (const field of CARD_FIELDS[kind] || []) {
    const raw = values[field.key] || '';
    if (field.list) {
      const items = raw.split('\n').map(line => line.trim()).filter(Boolean);
      if (!items.length) return `${field.label}: add at least one item.`;
      if (items.length > field.maxItems) return `${field.label}: at most ${field.maxItems} items.`;
      if (items.some(item => item.length > field.max)) return `${field.label}: each item is limited to ${field.max} characters.`;
    } else {
      if (field.required && !raw.trim()) return `${field.label} is required.`;
      if (raw.length > field.max) return `${field.label} is limited to ${field.max} characters.`;
    }
  }
  return '';
}
