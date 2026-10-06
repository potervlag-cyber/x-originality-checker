# Browser runtime notices

The published site includes unmodified Pyodide 0.27.7 and its CPython 3.12.7
runtime so visitors can evaluate their materials in their own browser.

- Pyodide: Mozilla Public License 2.0. The license is distributed in
  `vendor/pyodide/LICENSE.pyodide`. Corresponding source is available at
  <https://github.com/pyodide/pyodide/tree/0.27.7>.
- CPython: Python Software Foundation License Version 2 and the historical
  license notices in `vendor/pyodide/LICENSE.cpython`. Corresponding source is
  available at <https://github.com/python/cpython/tree/v3.12.7>.
- Upstream build components and notices are documented by the Pyodide release
  and source tree. No third-party Python packages are downloaded by this app.

The exact runtime URLs, byte lengths, and SHA-256 digests are recorded in
`deployment/runtime-lock.json` in the source repository. Runtime files are
served by the same GitHub Pages site as the application.
