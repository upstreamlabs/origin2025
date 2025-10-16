// @ts-check
import { defineConfig } from 'astro/config';

import sitemap from '@astrojs/sitemap';

// https://astro.build/config
export default defineConfig({
  integrations: [sitemap()],
  site: "https://upstreamignite.org",
  base: process.env.GITHUB_PAGES === 'true' ? "/origin-2025" : "/",
  i18n: {
    defaultLocale: "en",
    locales: ["en", "cn"],
    routing: {
      prefixDefaultLocale: true
    }
  },
  redirects: {
    '/schedule': '/en/agenda',
    '/agenda': '/en/agenda',
  },
});
