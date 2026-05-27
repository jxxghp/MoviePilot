# Firefox-based stealth option for browser helper (proposal)

> Status: Draft proposal
> Created: 2026-05-26
> Tracking discussion: TBD

## Overview

Optional Firefox-based stealth choice for `PLAYWRIGHT_BROWSER_TYPE`, parallel to the existing `chromium` and `firefox` values added in PR #5250. Selected via config, no change to defaults.

## Motivation

CloakBrowser is already wired into `app/helper/browser.py` as the Chromium-stealth path. Private trackers and some media-info sources historically work better when contacted by a Firefox UA, and a few have started rate-limiting CloakBrowser's Chromium UA pattern. A Firefox-stealth option lets operators pick the engine that fits each tracker without changing helper code.

## Proposed change

Add `invisible_firefox` as a third valid value for `PLAYWRIGHT_BROWSER_TYPE`. When selected, `PlaywrightHelper` resolves to `invisible_playwright` (https://github.com/feder-cr/invisible_playwright) which drives a patched Firefox 150 binary (https://github.com/feder-cr/invisible_firefox, MPL-2, same license as Firefox upstream). Fingerprint patches at the C++ source level so there are no JS shims to detect.

Drop-in compatible with the existing `BrowserContext` / `BrowserPage` / `BrowserElement` protocols in `app/helper/browser.py`. Optional dependency, only imported when the value is selected.

## Out of scope

No change to existing `chromium` or `firefox` values. No change to CloakBrowser path. No change to defaults.

## Maintenance

Issues against the backend route to feder-cr/invisible_playwright. Only ask of this repo would be the resolver branch in `PlaywrightHelper.__init__` plus a config docstring update.

---

## 简介

为 `PLAYWRIGHT_BROWSER_TYPE` 增加可选的 Firefox 隐身后端，与 PR #5250 增加的 `chromium` 和 `firefox` 选项并行。通过配置开启，不影响默认行为。详细方案见上方英文部分。
