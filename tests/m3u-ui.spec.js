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
}
