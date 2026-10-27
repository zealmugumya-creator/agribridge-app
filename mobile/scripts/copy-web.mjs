/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

// Copies the live web app (../static) into ./www so Capacitor can bundle it
// into the native iOS/Android app. Run automatically by `npm run build`,
// `npm run ios`, and `npm run android`. Keeps one source of truth: you edit
// static/index.html as usual, and this packages whatever is there.
import { cpSync, rmSync, mkdirSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = resolve(here, '..', '..', 'static');
const dest = resolve(here, '..', 'www');

if (!existsSync(src)) {
  console.error('✗ Could not find the web app at: ' + src);
  console.error('  Run this from inside the mobile/ folder of the agribridge repo.');
  process.exit(1);
}

rmSync(dest, { recursive: true, force: true });
mkdirSync(dest, { recursive: true });
cpSync(src, dest, { recursive: true });
console.log('✓ Packaged web app for native build:');
console.log('  ' + src);
console.log('  → ' + dest);
