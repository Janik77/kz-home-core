// Optional developer acceptance. Reads installed Playwright from Web; writes no Web files.
import { createRequire } from 'node:module';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import assert from 'node:assert/strict';

const [project, webSource] = process.argv.slice(2);
assert.match(project ?? '', /^kzhome-demo-check-[a-z0-9]+$/);
const folder = resolve('deploy/local/demo', project);
const credentials = JSON.parse(await readFile(resolve(folder, 'user.json'), 'utf8'));
const state = JSON.parse(await readFile(resolve(folder, 'state.json'), 'utf8'));
const require = createRequire(resolve(webSource, 'package.json'));
const { chromium } = require('playwright');
const browser = await chromium.launch({ channel: 'chrome', headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  const requests = [];
  page.on('pageerror', () => errors.push('runtime error'));
  page.on('request', r => { if (new URL(r.url()).pathname.startsWith('/core-api/')) requests.push({ path: new URL(r.url()).pathname, method: r.method() }); });
  const base = `http://127.0.0.1:${state.env.DEMO_WEB_PORT}`;
  await page.goto(base);
  await page.getByLabel('Email', { exact: true }).fill(credentials.email);
  await page.getByLabel('Password', { exact: true }).fill(credentials.password);
  await page.getByLabel('Password', { exact: true }).press('Enter');
  await page.getByRole('heading', { name: 'Home', level: 1 }).waitFor();
  await page.getByRole('article', { name: 'SIM · реле in SIM · гостиная' }).waitFor();
  assert.equal(await page.getByRole('switch').count(), 0);
  assert.equal(await page.getByRole('slider').count(), 0);
  assert.equal(await page.getByRole('button', { name: /Activate/ }).count(), 0);
  await page.getByRole('link', { name: 'All rooms' }).click();
  await page.getByRole('heading', { name: 'Rooms', level: 1 }).waitFor();
  await page.getByRole('link', { name: /SIM · спальня.*devices/ }).click();
  await page.getByRole('heading', { name: 'SIM · спальня', level: 1 }).waitFor();
  // Deep-link route must serve the real compiled app, not a synthetic test page.
  await page.reload();
  await page.getByRole('heading', { name: 'Welcome home' }).waitFor();
  await page.getByLabel('Email', { exact: true }).fill(credentials.email);
  await page.getByLabel('Password', { exact: true }).fill(credentials.password);
  await page.getByLabel('Password', { exact: true }).press('Enter');
  await page.getByRole('heading', { name: 'SIM · спальня', level: 1 }).waitFor();
  await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('link', { name: 'Scenes', exact: true }).click();
  await page.getByRole('article', { name: 'SIM · реле on', exact: true }).waitFor();
  await page.getByRole('link', { name: 'Account & homes' }).click();
  const logout = page.waitForResponse(r => r.url().endsWith('/core-api/auth/logout'));
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  assert.equal((await logout).status(), 204);
  await page.getByRole('heading', { name: 'Welcome home' }).waitFor();
  assert.deepEqual(errors, []);
  assert.ok(requests.every(r => r.method === 'GET' || ['/core-api/auth/login', '/core-api/auth/logout', '/core-api/auth/refresh'].includes(r.path)));
  console.log('PASS: real built Web login, rooms/deep link, scenes, logout and read-only browser requests; no interception.');
} finally {
  await browser.close();
}
