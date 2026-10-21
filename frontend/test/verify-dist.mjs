import { readFile, readdir } from 'node:fs/promises'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { createHash } from 'node:crypto'
const dir = fileURLToPath(new URL('../dist/assets/', import.meta.url))
const files = await readdir(dir)
const entry = await readFile(new URL('../dist/assets/remoteEntry.js', import.meta.url), 'utf8')
const manifest = JSON.parse(await readFile(new URL('../dist/assets/source-manifest.json', import.meta.url), 'utf8'))
for (const [source, hash] of Object.entries(manifest)) {
  const current = createHash('sha256').update(await readFile(new URL(`../${source}`, import.meta.url))).digest('hex')
  assert.equal(current, hash, `Built output is stale for source: ${source}`)
}
assert(!entry.includes('${__federation_expose_'), 'Federation output contains an unresolved expose placeholder')
for (const exposed of ['Page', 'AppPage', 'Config', 'MediaActions']) {
  assert(entry.includes(`"./${exposed}"`), `Missing expose: ${exposed}`)
  assert(files.some(file => file.startsWith(`__federation_expose_${exposed}-`) && file.endsWith('.js')), `Missing built module: ${exposed}`)
}
for (const reference of entry.matchAll(/(?:\.\/)?(__federation_expose_[\w-]+\.js|style-[\w-]+\.css)/g)) {
  assert(files.includes(reference[1]), `Broken federation asset: ${reference[1]}`)
}
console.log('Federation output verified: Page, AppPage, Config, MediaActions, assets and build-time source manifest.')
