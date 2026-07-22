const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests',
  testMatch: /m3u-ui\.spec\.js/,
  timeout: 60_000,
  use: {
    baseURL: process.env.M3U_BOSS_TEST_URL || 'http://127.0.0.1:43817',
    httpCredentials: {
      username: process.env.M3U_BOSS_ADMIN_USERNAME || 'admin',
      password: process.env.M3U_BOSS_ADMIN_PASSWORD || 'test-only-password',
    },
  },
});
