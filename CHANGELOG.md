## [1.1.0](https://github.com/gopaycommunity/gopay-python-api-v4/compare/1.0.0...1.1.0) (2026-11-25)

### Features

* carry the Retry-After wait on GoPayHTTPError as retry_after GPOMA-2752 ([a15bd3e](https://github.com/gopaycommunity/gopay-python-api-v4/commit/a15bd3e0fdac0f2e9281cdd0f44045a827c13aa7))
* list, fetch and page through the eshop's events GPOMA-2752 ([f9d83a4](https://github.com/gopaycommunity/gopay-python-api-v4/commit/f9d83a4be2c7e84f8b9c265f6297d264932cfb73))
* send correlation ids with every request and carry them on errors GPOMA-2752 ([5db3576](https://github.com/gopaycommunity/gopay-python-api-v4/commit/5db35768902c922a4b7f49bf8dedb00ca6e3f054))
* verify webhooks and return their events GPOMA-2752 ([1bf03bf](https://github.com/gopaycommunity/gopay-python-api-v4/commit/1bf03bfd1bbb02ab938f9bf3d2d1417ba0f66e39))

### Bug Fixes

* build with a hatchling that reads license-files, and test the wheel on the floors GPOMA-2752 ([fa07987](https://github.com/gopaycommunity/gopay-python-api-v4/commit/fa0798743e93e3fdc3783816464b7e93043cdb7c))
* fetch webhook keys without retries, forget a spent Retry-After GPOMA-2752 ([03648af](https://github.com/gopaycommunity/gopay-python-api-v4/commit/03648aff0e0b3ea299811e8935dc412a5e7e7a32))
* leave the wallet operations to the client SDKs GPOMA-2752 ([a152ddb](https://github.com/gopaycommunity/gopay-python-api-v4/commit/a152ddb4c24c722f5646f7b53c81dbc7fe8d8266))
* resolve SonarCloud issues in the transport and webhook code GPOMA-2752 ([00a4711](https://github.com/gopaycommunity/gopay-python-api-v4/commit/00a47117fd092c3964f99391d3f997c8e360947f))
* retry a 429 on reads as Retry-After asks, and spread retries with jitter GPOMA-2752 ([4e1843d](https://github.com/gopaycommunity/gopay-python-api-v4/commit/4e1843d3eab3504025663309ddad5c4b59d9e4fa))
* update api from next spec GPOMA-2752 ([6400165](https://github.com/gopaycommunity/gopay-python-api-v4/commit/6400165c9782266155bb785ddba96e1b4bf598aa))
* verify a webhook with a known fresh key without waiting on a refetch GPOMA-2752 ([06b043e](https://github.com/gopaycommunity/gopay-python-api-v4/commit/06b043ec605c84903dc86fd6977aa0847b02e8bc))

## 1.0.0 (2026-10-23)

### Features

* add the Payments API v4 client GPOMA-2692 ([c36bd55](https://github.com/gopaycommunity/gopay-python-api-v4/commit/c36bd55b522b7968c1163924565f9fc3b197705c))
* generate the API models from the spec GPOMA-2692 ([1516b0a](https://github.com/gopaycommunity/gopay-python-api-v4/commit/1516b0a602414b78f7be9a369bb6e55e08ada16b))

### Bug Fixes

* close the remaining review notes on docs and tests GPOMA-2692 ([59b2f78](https://github.com/gopaycommunity/gopay-python-api-v4/commit/59b2f7853a98031e26fbd85e3803ec17cda95748))
* follow the PHP SDK on AUTHORIZED, 3-D Secure and response bodies GPOMA-2692 ([85821db](https://github.com/gopaycommunity/gopay-python-api-v4/commit/85821dbb1143e0bbfdbd29fcd4f327235986fff1))
* keep credentials through transient auth failures and retry reads only GPOMA-2692 ([3221046](https://github.com/gopaycommunity/gopay-python-api-v4/commit/3221046ebae6faa58afc19a0c5badcbd364ecbc7))
* poll no faster than once a second GPOMA-2692 ([a7eb239](https://github.com/gopaycommunity/gopay-python-api-v4/commit/a7eb2393fc67212e76d3cc45833ca5450a58d38c))
* resolve SonarCloud issues GPOMA-2692 ([02a2a37](https://github.com/gopaycommunity/gopay-python-api-v4/commit/02a2a37dcf9cfe02a8b6579f8b56dcf028ef016d))
* resolve SonarCloud issues GPOMA-2692 ([0d053e6](https://github.com/gopaycommunity/gopay-python-api-v4/commit/0d053e62c6edc47305f8de1b0d937b3df0df1d2f))
* update certifi to 2026.10.19 GPOMA-2692 ([f721327](https://github.com/gopaycommunity/gopay-python-api-v4/commit/f7213275ddf68bf5c23b057eab1f98b8d21a32ac))
