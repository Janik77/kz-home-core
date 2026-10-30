// Actual compiled Web through nginx; no interception and no Web source writes.
import { createRequire } from 'node:module';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import assert from 'node:assert/strict';

const [project, web, mode] = process.argv.slice(2);
assert.match(project, /^kzhome-demo-check-v020[a-f0-9]+$/);
const folder = resolve('deploy/local/demo', project);
const state = JSON.parse(await readFile(resolve(folder, 'state.json'), 'utf8'));
const account = JSON.parse(await readFile(resolve(folder, 'user.json'), 'utf8'));
const require = createRequire(resolve(web, 'package.json'));
const { chromium } = require('playwright');
const { expect } = require('@playwright/test');
const browser = await chromium.launch({ channel: 'chrome' });
try {
  const page = await browser.newPage();
  let posts = 0;
  page.on('request', r => { if (r.method() === 'POST' && r.url().endsWith('/commands')) posts++; });
  await page.goto(`http://127.0.0.1:${state.env.DEMO_WEB_PORT}`);
  const permissionRead = page.waitForResponse(r => r.url().endsWith('/me/permissions'));
  await page.getByLabel('Email', { exact: true }).fill(account.email);
  await page.getByLabel('Password', { exact: true }).fill(account.password);
  await page.getByLabel('Password', { exact: true }).press('Enter');
  const card = page.getByRole('article').filter({ has: page.getByRole('heading', { name: 'SIM · реле', exact: true }) });
  await card.waitFor();
  const on = card.getByRole('button', { name: 'Включить', exact: true });
  const off = card.getByRole('button', { name: 'Выключить', exact: true });
  const status = card.getByRole('status');
  const shown = card.locator('.signature-device__state');
  const permission = await permissionRead;
  assert.equal((await permission.json()).permissions.includes('device.control'), true);
  if (mode === 'offline') {
    assert.equal(await on.isDisabled(), true);
    assert.equal(await off.isDisabled(), true);
    assert.equal(posts, 0);
  } else {
    await on.waitFor({ state: 'visible' });
    const accepted = page.waitForResponse(r => r.request().method() === 'POST' && r.url().endsWith('/commands'));
    await on.click();
    assert.equal((await accepted).status(), 202);
    if (mode === 'apply') {
      await status.filter({ hasText: 'Выполнено · данные Core обновлены' }).waitFor();
      assert.equal(await shown.textContent(), 'Включено');
      for (let i = 0; i < 2; i++) {
        await expect(off).toBeEnabled();
        const acceptedOff = page.waitForResponse(r => r.request().method() === 'POST' && r.url().endsWith('/commands'));
        await off.click();
        assert.equal((await acceptedOff).status(), 202);
        await expect(off).toBeEnabled();
        await expect(status).toHaveText('Выполнено · данные Core обновлены');
        await expect(shown).toHaveText('Выключено');
      }
      assert.equal(posts, 3);
    } else if (mode === 'fail') {
      await status.filter({ hasText: 'Не удалось выполнить' }).waitFor();
      assert.equal(await shown.textContent(), 'Выключено');
      assert.equal(posts, 1);
    } else {
      await status.filter({ hasText: 'Ожидаем подтверждение' }).waitFor();
      assert.equal(await shown.textContent(), 'Выключено');
      assert.equal(await off.isDisabled(), true);
      await on.evaluate(e => { e.click(); e.click(); });
      await status.filter({ hasText: 'Нет подтверждения — состояние неизвестно' }).waitFor({ timeout: 40000 });
      assert.equal(await shown.textContent(), 'Выключено');
      assert.equal(posts, 1);
    }
  }
  assert.equal(await page.evaluate(async () => (await fetch('/core-api/v1/devices/e2e_relay/commands', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID() }, body: '{"on":true}' })).status), 401);
  await page.getByRole('link', { name: 'Аккаунт и дома' }).click();
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await page.getByLabel('Email', { exact: true }).waitFor();
  console.log(`PASS compiled demo Web ${mode}, actual auth/permissions/HTTP; software only`);
} catch {
  console.log('FAIL compiled demo browser acceptance; sensitive details withheld');
  process.exitCode = 1;
} finally { await browser.close(); }
