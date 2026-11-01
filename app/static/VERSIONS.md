# Vendored static assets

Third-party scripts are served from here so no page loads script from another host.
Do not put journal data under this folder: `/static/*` is public (no session).

| File | Source | Version | sha256 |
| --- | --- | --- | --- |
| `htmx-2.0.4.min.js` | https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js | htmx 2.0.4 | e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447 |
| `tailwind-play.js` | https://cdn.tailwindcss.com/3.4.17 | Tailwind Play CDN 3.4.17 | 176e894661aa9cdc9a5cba6c720044cbbf7b8bd80d1c9a142a7c24b1b6c50d15 |
| `fonts/inter-latin-variable.woff2` | https://fonts.gstatic.com/l/font?kit=UcCB3FwrK3iLTeHuS_nVMrMxCp50SjIw2boKoduKmMEVuLyfU5NJeWUdIOjBeTGbXZdND2M&skey=c491285d6722e4fa&v=v20 | Inter v20, latin, variable (weights 400-600 used) | 76658c751953637fc8bd972c0c8581103ff50fd1a301c016c6ee8cbe2d651db6 |
| `fonts/instrument-serif-400-latin.woff2` | https://fonts.gstatic.com/s/instrumentserif/v5/jizBRFtNs2ka5fXjeivQ4LroWlx-6zUTjg.woff2 | Instrument Serif v5, regular 400, latin | 5eb09b5ac0e28b67c2f041c8ba6d244604ca0c0980d65912ab2d47fed84ddc31 |

Both fonts are licensed under the SIL Open Font License 1.1 (OFL), which allows self-hosting.
They came from the Google Fonts CSS2 API (Inter 400/500/600, Instrument Serif 400, latin subset only;
Google serves one variable file for all three Inter weights). The pages no longer contact Google.

The htmx file matches the sha384 `integrity` attribute in `app/templates/base.html`
(checked in `tests/test_static.py`). Downloaded 2026-11-01.
