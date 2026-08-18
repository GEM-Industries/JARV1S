#!/usr/bin/env node
/**
 * Fail if a v* git tag does not match backend/pyproject.toml.
 * Usage: node verify-release-tag.mjs [vX.Y.Z]
 */
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '../../..')
const pyproject = readFileSync(join(root, 'backend/pyproject.toml'), 'utf8')
const version = pyproject.match(/^version = "(.+)"$/m)?.[1]
if (!version) {
  console.error('Could not read backend version from pyproject.toml')
  process.exit(1)
}

const tag = process.argv[2] || process.env.GITHUB_REF_NAME || ''
if (!tag) {
  console.error('Missing release tag (pass vX.Y.Z or set GITHUB_REF_NAME)')
  process.exit(1)
}

const expected = `v${version}`
if (tag !== expected) {
  console.error(`Tag/version mismatch: tag=${tag} expected=${expected}`)
  process.exit(1)
}

console.log(`Release tag OK: ${tag}`)
