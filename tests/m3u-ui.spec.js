const { test, expect } = require('@playwright/test');

for (const viewport of [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'mobile', width: 393, height: 727 },
]) {
  test(`${viewport.name} clean install loads the editor`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const failures = [];
    page.on('pageerror', error => failures.push(error.message));
    page.on('response', response => {
      if (response.url().includes('/api/') && response.status() >= 500)
        failures.push(`${response.status()} ${response.url()}`);
      if (response.url().includes('/static/') && response.status() >= 400)
        failures.push(`${response.status()} ${response.url()}`);
    });

    await page.goto('/#editor', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('#page-editor')).toBeVisible();
    await expect(page.locator('#groups-list')).toBeVisible();
    await expect(page.locator('#group-view-filter')).toHaveValue('active');
    expect(failures).toEqual([]);
  });

  test(`${viewport.name} Safety Center loads all workflows`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const failures = [];
    page.on('pageerror', error => failures.push(error.message));
    page.on('response', response => {
      if (response.url().includes('/api/') && response.status() >= 500)
        failures.push(`${response.status()} ${response.url()}`);
    });
    await page.goto('/#safety', { waitUntil: 'networkidle' });
    await expect(page.locator('#page-safety')).toBeVisible();
    await expect(page.locator('#setup-checklist .safety-check')).toHaveCount(6);
    await expect(page.locator('#migration-health')).toContainText('Schema 3 is current');
    await expect(page.locator('#teamarr-compatibility')).not.toContainText('Loading');
    await expect(page.locator('#teamarr-compatibility')).toContainText('Teamarr enabled');
    await page.locator('#snapshot-label').fill(`${viewport.name} browser snapshot`);
    await page.locator('#snapshot-create').click();
    await expect(page.locator('#snapshot-list')).toContainText(`${viewport.name} browser snapshot`);
    expect(failures).toEqual([]);
  });
}
