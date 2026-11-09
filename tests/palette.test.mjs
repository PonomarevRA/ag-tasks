import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

/** Read actual theme custom properties rather than duplicating their current values in the assertions. */
function palette(block) {
  const result = {};
  for (const [,key,value] of block.matchAll(/--([\w-]+):\s*(#[0-9a-f]{3,6})/gi)) result[key] = value;
  return result;
}

/** Convert sRGB channels to relative luminance using the WCAG contrast formula. */
function luminance(hex) {
  const expanded = hex.length === 4 ? '#' + [...hex.slice(1)].map(value=>value+value).join('') : hex;
  const linear = [1,3,5].map(index => {
    const channel = Number.parseInt(expanded.slice(index,index+2),16) / 255;
    return channel <= .04045 ? channel / 12.92 : ((channel+.055)/1.055)**2.4;
  });
  return linear[0]*.2126 + linear[1]*.7152 + linear[2]*.0722;
}

/** Compare actual foreground/background pairs; this test does not certify the whole interface. */
function contrast(foreground, background) {
  const values = [luminance(foreground),luminance(background)].sort((a,b)=>a-b);
  return (values[1]+.05)/(values[0]+.05);
}

test('actual light and dark text/primary/selected navigation pairs retain at least 4.5:1 contrast', async () => {
  const source = await readFile(new URL('../web/styles.css',import.meta.url),'utf8');
  const light = palette(source.match(/:root\{([^}]*)\}/)[1]);
  const dark = palette(source.match(/@media\(prefers-color-scheme:dark\)\{:root\{([^}]*)\}/)[1]);
  const pairs = [];
  for (const [name,theme,onAccent] of [['light',light,'#fff'],['dark',dark,'#111825']]) {
    for (const background of ['panel','subtle']) {
      pairs.push([`${name} text/${background}`,theme.text,theme[background]]);
      pairs.push([`${name} muted/${background}`,theme.muted,theme[background]]);
    }
    pairs.push([`${name} primary`,onAccent,theme.blue]);
    pairs.push([`${name} selected navigation`,theme.blue,theme.selected]);
  }
  for (const [name,foreground,background] of pairs) assert.ok(contrast(foreground,background)>=4.5,`${name}: ${contrast(foreground,background).toFixed(3)}:1`);
});
