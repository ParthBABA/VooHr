/* Run with NODE_PATH pointing at a Playwright installation. Uses mocked API data. */
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const root = path.resolve(__dirname, '..');
const files = fs.readdirSync(path.join(root, 'static')).filter(f => f.endsWith('.html'));
const server = spawn(process.env.PYTHON || 'python', ['scripts/preview_ui.py'], {cwd: root, stdio: 'ignore'});
const origin = 'http://127.0.0.1:5099';
const me = {id:'111111111111111111111111', name:'Alex Morgan', email:'alex@example.test', role:'admin', organization:{name:'Example organization',industry:'Technology',company_size:'11-50'}};
const failures = [];

(async () => {
  for (const file of files) {
    const source = fs.readFileSync(path.join(root, 'static', file), 'utf8');
    for (const match of source.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
      if (!/\bsrc=|application\/ld\+json|application\/json/i.test(match[1])) new vm.Script(match[2], {filename: file});
    }
  }
  for (let i = 0; i < 60; i++) {
    try { if ((await fetch(origin)).ok) break; } catch {}
    await new Promise(r => setTimeout(r, 250));
  }
  const browser = await chromium.launch({channel:'msedge', headless:true});
  try {
    const context = await browser.newContext({reducedMotion:'reduce'});
    await context.addInitScript(() => localStorage.setItem('cookieConsent', 'true'));
    await context.route('**/api/**', route => {
      const url = new URL(route.request().url());
      let data = {ok:true, employees:[],sessions:[],meetings:[],notifications:[],alerts:[],items:[],total:0,has_more:false};
      if (url.pathname === '/api/me') data = me;
      if (url.pathname === '/api/csrf-token') data = {csrf_token:'test-token'};
      return route.fulfill({json:data});
    });
    await context.route('**/auth/**', route => route.fulfill({json:{ok:false,error:'invalid_credentials'}}));
    const page = await context.newPage();
    page.on('dialog', dialog => { failures.push('Unexpected native dialog: ' + dialog.message()); dialog.dismiss(); });
    for (const file of [...files, 'landing']) {
      const errors = [];
      const onError = error => errors.push(error.message);
      page.on('pageerror', onError);
      await page.goto(file === 'landing' ? origin : origin + '/preview/' + file, {waitUntil:'domcontentloaded'});
      await page.waitForTimeout(600);
      for (const width of [480,768,1024,1280]) {
        await page.setViewportSize({width,height:900});
        await page.waitForTimeout(50);
        const overflow = await page.evaluate(() => {
          return [...document.querySelectorAll('body *')].filter(el => {
            const rect = el.getBoundingClientRect();
            const css = getComputedStyle(el);
            if (!rect.width || !rect.height || css.visibility === 'hidden' || css.opacity === '0') return false;
            if (rect.right <= innerWidth + 2 || rect.left < 0) return false;
            for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
              const style = getComputedStyle(p);
              if (['auto','scroll','hidden','clip'].includes(style.overflowX) || style.opacity === '0') return false;
            }
            return true;
          }).slice(0,6).map(el => el.tagName + '.' + el.className);
        });
        if (overflow.length) failures.push(`${file} @ ${width}: overflow ${overflow.join(', ')}`);
      }
      page.off('pageerror', onError);
      if (errors.length) failures.push(file + ': JS errors ' + [...new Set(errors)].join('; '));
    }
    await page.goto(origin + '/preview/login2.html');
    await page.locator('#loginForm [type=submit]').click();
    assert.match(await page.locator('[role=alert]').innerText(), /both email and password/);
    await page.locator('#emailInput').fill('alex@example.test');
    await page.locator('#passwordInput').fill('Incorrect123');
    await page.locator('#loginForm [type=submit]').click();
    await page.getByText('Invalid email or password. Please try again.', {exact:true}).waitFor();
    await context.route('**/auth/email/signin', route => route.fulfill({json:{ok:true,requires_otp:true}}));
    await page.locator('#loginForm [type=submit]').click();
    await page.locator('#otpInput').waitFor({state:'visible'});
    await page.locator('#otpInput').fill('123456');
    await page.locator('#verifyBtn').click();
    await page.getByText('That code is incorrect. Please try again.', {exact:true}).waitFor();
    await page.locator('#changeEmailLink').click();
    assert.equal(await page.locator('#emailInput').evaluate(el => el === document.activeElement), true);
    await page.evaluate(() => { window.confirmed = null; VooVrUI.ask('Discard text?', {accept:'Discard'}).then(value => window.confirmed = value); });
    await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(() => window.confirmed), false);
    await page.evaluate(() => { VooVrUI.ask('Discard text?', {accept:'Discard'}).then(value => window.confirmed = value); });
    await page.locator('[data-accept]').click();
    assert.equal(await page.evaluate(() => window.confirmed), true);
    await page.evaluate(() => VooVrUI.show('<img src=x onerror=alert(1)>'));
    assert.equal(await page.locator('[role=alert] img').count(), 0);
    for (const theme of ['dark','light']) {
      await page.evaluate(theme => voovrSetTheme(theme), theme);
      await page.waitForTimeout(100);
      assert.equal(await page.evaluate(() => getComputedStyle(document.querySelector('.auth-card')).backgroundColor), theme === 'dark' ? 'rgb(19, 23, 31)' : 'rgb(255, 255, 255)');
    }
    await page.goto(origin + '/preview/signup.html');
    await page.locator('#signupBtn').click();
    await page.getByText('Please enter your organization name, industry, and company size.', {exact:true}).waitFor();
    await page.locator('#orgNameInput').fill('Example organization');
    await page.locator('#industryInput').fill('Technology');
    await page.locator('#companySizeInput').selectOption('11-50');
    await page.locator('#emailInput').fill('alex@example.test');
    await page.locator('#passwordInput').fill('Strong123');
    await page.locator('#confirmPasswordInput').fill('Mismatch123');
    await page.locator('#signupBtn').click();
    await page.getByText('Your passwords do not match.', {exact:true}).waitFor();
    await page.locator('#confirmPasswordInput').fill('Strong123');
    await page.locator('#termsCheckbox').check();
    await context.route('**/auth/email/start', route => route.fulfill({json:{ok:false,error:'already_registered'},status:409}));
    await page.locator('#signupBtn').click();
    await page.getByText('This email is already registered. Please sign in.', {exact:true}).waitFor();
    await context.route('**/auth/forgot-password', route => route.fulfill({json:{ok:false,error:'reset_unavailable',message:'Password reset emails are not available yet.'},status:503}));
    await page.goto(origin + '/preview/forgot-password.html');
    await page.locator('#emailInput').fill('alex@example.test');
    await page.locator('[type=submit]').click();
    await page.getByText('Password reset emails are not available yet.', {exact:true}).waitFor();

    // Directory pagination must include employees beyond the server's 200-row cap.
    const employees = Array.from({length:205}, (_, index) => ({id:String(index),name:'Employee ' + index,department:'Engineering',status:'active',wellness_score:null}));
    await context.route('**/api/employees?*', route => {
      const pageNumber = Number(new URL(route.request().url()).searchParams.get('page') || 1);
      return route.fulfill({json:{employees:employees.slice((pageNumber-1)*200,pageNumber*200),total:205,has_more:pageNumber===1}});
    });
    await page.goto(origin + '/preview/dashboard.html');
    await page.waitForFunction(() => document.querySelectorAll('#dashEmpTable tbody tr').length === 205);
    assert.equal(await page.locator('#dashEmpTable tbody tr:visible').count(), 20);
    await page.locator('#dashNextPage').click();
    assert.match(await page.locator('#dashEmpFooterText').innerText(), /21 to 40 of 205/i);
    await page.locator('#dashDirSearch').fill('Employee 204');
    assert.equal(await page.locator('#dashEmpTable tbody tr:visible').count(), 1);
    assert.match(await page.locator('#dashEmpFooterText').innerText(), /1 to 1 of 1/i);
    await context.route('**/auth/totp/status', route => route.fulfill({json:{totp_enabled:true}}));
    await context.route('**/auth/totp/backup-codes-status', route => route.fulfill({json:{has_backup_codes:true,codes_remaining:8}}));
    let regenerated = 0;
    await context.route('**/auth/totp/regenerate-backup-codes', route => {
      regenerated++;
      return route.fulfill({json:{backup_codes:['AAAA-BBBB','CCCC-DDDD']}});
    });
    await page.goto(origin + '/preview/settings.html#security');
    await page.locator('#regenerateCodesBtn').click();
    await page.locator('[data-cancel]').click();
    assert.equal(regenerated, 0);
    await page.locator('#regenerateCodesBtn').click();
    await page.locator('[data-accept]').click();
    await page.locator('.recovery-code').first().waitFor();
    assert.equal(regenerated, 1);
    assert.equal(await page.locator('.recovery-code').count(), 2);
    await page.getByRole('button', {name:'Done', exact:true}).click();
    assert.equal(await page.locator('.recovery-dialog').count(), 0);
    console.log(JSON.stringify({pages:files.length+1,widths:[480,768,1024,1280],failures},null,2));
    if (failures.length) process.exitCode = 1;
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1;}).finally(() => server.kill());
