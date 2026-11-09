/** Derive a stable accent from the normalized name plus namespace; text keeps the service's contrast-safe colour. */
export function nameColor(name, namespace = '') {
  const source = `${namespace.normalize('NFKC').trim().toLocaleLowerCase('en')}\u0000${name.normalize('NFKC').trim().toLocaleLowerCase('en')}`;
  let hash = 2166136261;
  for (const character of source) {
    hash ^= character.codePointAt(0);
    hash = Math.imul(hash, 16777619) >>> 0;
  }
  return `hsl(${hash % 360} ${52 + (hash >>> 9) % 17}% ${44 + (hash >>> 18) % 13}%)`;
}

/** Colour is a redundant edge/swatch cue, never the sole task or space identifier. */
export function applyIdentity(node, name, namespace = '') {
  node.style.setProperty('--identity-color', nameColor(name || '', namespace));
  node.classList.add('named-identity');
}
