import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { readFileSync, unlinkSync, writeFileSync } from 'node:fs'
import test from 'node:test'

const script = new URL('./publish-github-release.sh', import.meta.url)
const version = JSON.parse(
  readFileSync(new URL('../src-tauri/tauri.conf.json', import.meta.url), 'utf8'),
).version

function run(args, env) {
  return spawnSync('bash', [script.pathname, ...args], {
    encoding: 'utf8',
    env: env ? { ...process.env, ...env } : process.env,
  })
}

test('help describes the private/public audience split', () => {
  const result = run(['--help'])
  assert.equal(result.status, 0, result.stderr)
  assert.match(result.stdout, /--private/)
  assert.match(result.stdout, /--public/)
  assert.match(result.stdout, /Beta audience/)
  assert.match(result.stdout, /no updater channel/)
  assert.match(result.stdout, /CHANGELOG.md/)
  assert.match(result.stdout, /Source code zip/)
})

test('unknown flags fail closed', () => {
  const result = run(['--both'])
  assert.notEqual(result.status, 0)
  assert.match(result.stderr, /Unknown argument/)
})

test('print-changelog emits the current version section only', () => {
  const result = run(['--print-changelog'])
  assert.equal(result.status, 0, result.stderr)
  assert.ok(result.stdout.startsWith(`## [${version}]`), result.stdout)
  assert.doesNotMatch(result.stdout, /Unreleased/)
  assert.doesNotMatch(result.stdout, /^\[[^\]]+\]:/m)
})

test('print-changelog fails closed when the version section is missing', () => {
  const changelog = new URL('./publish-github-release.missing-changelog.md', import.meta.url)
  writeFileSync(changelog, '# Changelog\n\n## [Unreleased]\n\n')
  try {
    const result = run(['--print-changelog'], { JARVIS_CHANGELOG: changelog.pathname })
    assert.notEqual(result.status, 0)
    assert.match(result.stderr, new RegExp(`no ## \\[${version.replaceAll('.', '\\.')}\\] section`))
  } finally {
    unlinkSync(changelog)
  }
})
