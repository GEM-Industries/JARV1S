import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'
import test from 'node:test'

const script = new URL('./generate-update-manifest.mjs', import.meta.url)

function fixture() {
  const root = mkdtempSync(join(tmpdir(), 'jarv1s-update-'))
  const macos = join(root, 'macos')
  mkdirSync(macos)
  writeFileSync(join(macos, 'JARV1S.app.tar.gz'), 'archive')
  writeFileSync(join(macos, 'JARV1S.app.tar.gz.sig'), 'signed-value\n')
  return root
}

function run(args, env = {}) {
  return spawnSync(process.execPath, [script.pathname, ...args], {
    encoding: 'utf8',
    env: { ...process.env, ...env },
  })
}

test('writes an absolute HTTPS internal manifest', () => {
  const root = fixture()
  const result = run([root, '0.2.0', 'internal', 'https://updates.example/releases'])
  assert.equal(result.status, 0, result.stderr)

  const manifest = JSON.parse(readFileSync(join(root, 'latest.json'), 'utf8'))
  assert.equal(manifest.version, '0.2.0')
  assert.equal(manifest.platforms['darwin-aarch64'].signature, 'signed-value')
  assert.equal(
    manifest.platforms['darwin-aarch64'].url,
    'https://updates.example/releases/JARV1S.app.tar.gz',
  )
})

test('rejects insecure updater origins', () => {
  const root = fixture()
  const result = run([root, '0.2.0', 'beta', 'http://updates.example'])
  assert.notEqual(result.status, 0)
  assert.match(result.stderr, /absolute HTTPS/)
})

test('requires an absolute URL in CI', () => {
  const root = fixture()
  const result = run([root, '0.2.0'], { CI: 'true' })
  assert.notEqual(result.status, 0)
  assert.match(result.stderr, /required in CI/)
})

test('rejects unsupported channels and invalid versions', () => {
  const root = fixture()
  assert.notEqual(run([root, 'next', 'internal']).status, 0)
  assert.notEqual(run([root, '0.2.0', 'stable']).status, 0)
})
