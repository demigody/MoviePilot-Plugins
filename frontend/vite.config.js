import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import federation from '@originjs/vite-plugin-federation'
import { createHash } from 'node:crypto'
import { readdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = dirname(fileURLToPath(import.meta.url))
async function sourceFiles(directory = 'src') {
  const entries = await readdir(join(root, directory), { withFileTypes: true })
  const nested = await Promise.all(entries.map(entry => entry.isDirectory()
    ? sourceFiles(`${directory}/${entry.name}`) : [`${directory}/${entry.name}`]))
  return nested.flat()
}
function sourceManifest() {
  return { name: 'build-source-manifest', apply: 'build', async closeBundle() {
    const files = [...await sourceFiles(), 'package.json', 'package-lock.json', 'vite.config.js', 'index.html', 'postcss.config.js'].sort()
    const entries = await Promise.all(files.map(async path => [path, createHash('sha256').update(await readFile(join(root, path))).digest('hex')]))
    await writeFile(join(root, 'dist/assets/source-manifest.json'), `${JSON.stringify(Object.fromEntries(entries), null, 2)}\n`)
  } }
}

export default defineConfig(({ command }) => ({
  // Development previews provide their own Vuetify CSS; federation builds
  // reuse the host styles through postcss.config.js instead.
  css: command === 'serve' ? { postcss: { plugins: [] } } : {},
  plugins: [
    vue(),
    sourceManifest(),
    federation({
      name: 'EmbyCollectionSync',
      filename: 'remoteEntry.js',
      exposes: { './Page': './src/Page.vue', './AppPage': './src/AppPage.vue', './Config': './src/Config.vue', './MediaActions': './src/MediaActions.vue' },
      shared: {
        vue: { requiredVersion: false, generate: false, singleton: true },
        vuetify: { requiredVersion: false, generate: false, singleton: true },
        'vuetify/styles': { requiredVersion: false, generate: false, singleton: true },
      },
    }),
  ],
  build: {
    target: 'esnext',
    outDir: 'dist/assets',
    assetsDir: '',
    emptyOutDir: true,
    cssCodeSplit: false,
  },
}))
