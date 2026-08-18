#!/usr/bin/env node
/**
 * Generate static latest.json for Tauri updater.
 * Usage: node generate-update-manifest.mjs <bundle-dir> <version> [channel] [baseUrl]
 */
import { existsSync, readFileSync, writeFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

const [bundleDir, version, channel = 'internal', baseUrl = ''] = process.argv.slice(2)
if (!bundleDir || !version) {
  console.error('Usage: generate-update-manifest.mjs <bundle-dir> <version> [channel] [baseUrl]')
  process.exit(1)
}
if (!/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(version)) {
  console.error(`Version must be SemVer, got: ${version}`)
  process.exit(1)
}
if (!['internal', 'beta'].includes(channel)) {
  console.error(`Unsupported updater channel: ${channel}`)
  process.exit(1)
}

if (!baseUrl && process.env.CI === 'true') {
  console.error('JARVIS_UPDATE_BASE_URL / baseUrl is required in CI so updater URLs are absolute')
  process.exit(1)
}

const macDir = join(bundleDir, 'macos')
const files = readdirSync(macDir)
const tar = files.find((f) => f.endsWith('.app.tar.gz'))
const sigFile = tar ? `${tar}.sig` : null
if (!tar || !sigFile || !existsSync(join(macDir, sigFile))) {
  console.error('Updater artifacts not found in', macDir)
  process.exit(1)
}

const signature = readFileSync(join(macDir, sigFile), 'utf8').trim()
const url = baseUrl ? `${baseUrl.replace(/\/$/, '')}/${tar}` : tar
if (baseUrl && !/^https:\/\//.test(url)) {
  console.error(`Updater URL must be absolute HTTPS, got: ${url}`)
  process.exit(1)
}

const manifestName = channel === 'internal' ? 'latest.json' : `latest-${channel}.json`
const manifest = {
  version,
  notes: `JARV1S ${version} (${channel})`,
  pub_date: new Date().toISOString(),
  platforms: {
    'darwin-aarch64': { signature, url },
  },
}

const out = join(bundleDir, manifestName)
writeFileSync(out, `${JSON.stringify(manifest, null, 2)}\n`)
console.log('Wrote', out)
