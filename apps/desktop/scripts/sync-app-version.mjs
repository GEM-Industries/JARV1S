#!/usr/bin/env node
import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '../../..')
const pyproject = join(root, 'backend/pyproject.toml')
const version = readFileSync(pyproject, 'utf8').match(/^version = "(.+)"$/m)?.[1]
if (!version) {
  console.error('Could not read backend version from pyproject.toml')
  process.exit(1)
}

const services = JSON.parse(
  readFileSync(join(root, 'apps/desktop/services/versions.json'), 'utf8'),
)
const minimumMacos = services.minimum_macos
if (typeof minimumMacos !== 'string' || !/^\d+\.\d+$/.test(minimumMacos)) {
  console.error('services/versions.json must define minimum_macos as major.minor')
  process.exit(1)
}

const channel = process.env.JARVIS_RELEASE_CHANNEL || 'internal'
if (!['internal', 'beta'].includes(channel)) {
  console.error(`Unsupported updater channel: ${channel}`)
  process.exit(1)
}
const manifestName = channel === 'internal' ? 'latest.json' : `latest-${channel}.json`
const updateBaseUrl = (
  process.env.JARVIS_UPDATE_BASE_URL
  || `https://github.com/GTS-html77/JARV1S/releases/download/${channel}`
).replace(/\/$/, '')
if (!/^https:\/\//.test(updateBaseUrl)) {
  console.error(`Updater base URL must be absolute HTTPS, got: ${updateBaseUrl}`)
  process.exit(1)
}

function writeIfChanged(path, next) {
  let prev = ''
  try {
    prev = readFileSync(path, 'utf8')
  } catch {
    prev = ''
  }
  if (prev === next) {
    return false
  }
  writeFileSync(path, next)
  return true
}

for (const rel of ['apps/desktop/package.json']) {
  const path = join(root, rel)
  const json = JSON.parse(readFileSync(path, 'utf8'))
  json.version = version
  if (writeIfChanged(path, `${JSON.stringify(json, null, 2)}\n`)) {
    console.log(`Synced ${rel} -> ${version}`)
  } else {
    console.log(`Unchanged ${rel} -> ${version}`)
  }
}

const tauriPath = join(root, 'apps/desktop/src-tauri/tauri.conf.json')
const tauri = JSON.parse(readFileSync(tauriPath, 'utf8'))
tauri.version = version
tauri.bundle.macOS.minimumSystemVersion = minimumMacos
tauri.plugins.updater.endpoints = [`${updateBaseUrl}/${manifestName}`]
if (writeIfChanged(tauriPath, `${JSON.stringify(tauri, null, 2)}\n`)) {
  console.log(`Synced apps/desktop/src-tauri/tauri.conf.json -> ${version}`)
} else {
  console.log(`Unchanged apps/desktop/src-tauri/tauri.conf.json -> ${version}`)
}

const cargoPath = join(root, 'apps/desktop/src-tauri/Cargo.toml')
const cargo = readFileSync(cargoPath, 'utf8')
const nextCargo = cargo.replace(/^version = "[^"]+"/m, `version = "${version}"`)
if (nextCargo === cargo && !cargo.includes(`version = "${version}"`)) {
  console.error('Could not update Cargo.toml package version')
  process.exit(1)
}
if (writeIfChanged(cargoPath, nextCargo)) {
  console.log(`Synced apps/desktop/src-tauri/Cargo.toml -> ${version}`)
} else {
  console.log(`Unchanged apps/desktop/src-tauri/Cargo.toml -> ${version}`)
}

console.log(`minimumSystemVersion -> ${minimumMacos}`)
console.log(`updater endpoint -> ${updateBaseUrl}/${manifestName}`)
