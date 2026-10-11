# Third-Party Notices

This project depends on open-source software listed below. Generated
2026-09-03 with `pip-licenses --format=markdown --with-urls` (Python,
resolved from `requirements.txt` plus the `voice`/`dev`/`desktop`
extras in `setup.py`, transitive dependencies included) and the npm
registry's `latest` metadata for each package named in
`dashboard/package.json` (direct dependencies and devDependencies;
transitive npm dependencies were not enumerated — `node_modules` was
not installed in this pass, see note at the bottom).

License strings are as reported by each package's own metadata and may
use different SPDX conventions (e.g. "MIT License" vs "MIT"); they are
reproduced verbatim rather than normalized.

---

## Upstream template

The dashboard did not start from an empty Next.js app. Its layout, its
Tailwind design tokens and a number of its UI components are derived from
an open-source admin template, and that is the single largest borrowed
work in this repository — larger than any one package in the tables
below.

| Name | License | Source | What we use |
|---|---|---|---|
| admin-one-react-tailwind | MIT | https://github.com/justboil/admin-one-react-tailwind | The dashboard's layout shell, Tailwind theme and several base UI components under `dashboard/app/_components/`, modified. |

The template's own MIT license text ships verbatim at `dashboard/LICENSE`,
which is what MIT §1 requires. This entry is here because a credits file
that omits the biggest borrow is not a credits file.

---

## Bundled assets and vendored code

Files that ship in this repository but were not written here.

| Name | License | Source | Where it ships |
|---|---|---|---|
| Material Symbols Rounded (variable icon font) | Apache-2.0 (Google, `google/material-design-icons`) | https://github.com/google/material-design-icons | `dashboard/public/fonts/material-symbols-rounded.woff2`, served self-hosted by `dashboard/css/_theme.css` |
| React 18.2.0 (UMD production build) | MIT (license header in the file itself) | https://github.com/facebook/react | `dashboard/public/vendor/js/react.production.min.js` |
| ReactDOM 18.2.0 (UMD production build) | MIT (license header in the file itself) | https://github.com/facebook/react | `dashboard/public/vendor/js/react-dom.production.min.js` |
| Babel standalone 7.23.9 (`@babel/standalone`, minified build) | MIT (Copyright (c) 2014-present Sebastian McKenzie and other contributors) | https://github.com/babel/babel | `dashboard/public/vendor/js/babel.min.js`, used by `fitfam-app.html` to compile its JSX in the browser |
| Baloo 2 (latin subset, woff2) | SIL OFL 1.1 (Ek Type) | https://github.com/EkType/Baloo2 | `dashboard/public/vendor/fonts/baloo2-*.woff2`, declared in `dashboard/public/vendor/fonts/fonts.css` |
| Exo 2 (latin subset, woff2) | SIL OFL 1.1 (Natanael Gama) | https://github.com/NDISCOVER/Exo-2.0 | `dashboard/public/vendor/fonts/exo2-*.woff2`, declared in `dashboard/public/vendor/fonts/fonts.css` |
| Fira Code (latin subset, woff2) | SIL OFL 1.1 (The Fira Code Project Authors) | https://github.com/tonsky/FiraCode | `dashboard/public/vendor/fonts/firacode-*.woff2`, declared in `dashboard/public/vendor/fonts/fonts.css` |
| Nunito (latin subset, woff2) | SIL OFL 1.1 (The Nunito Project Authors) | https://github.com/googlefonts/nunito | `dashboard/public/vendor/fonts/nunito-*.woff2`, declared in `dashboard/public/vendor/fonts/fonts.css` |
| Quicksand (latin subset, woff2) | SIL OFL 1.1 (The Quicksand Project Authors) | https://github.com/andrew-paglinawan/QuicksandFamily | `dashboard/public/vendor/fonts/quicksand-*.woff2`, declared in `dashboard/public/vendor/fonts/fonts.css` |

The standalone app pages load these fonts from `dashboard/public/vendor/fonts/`,
with the SIL Open Font License alongside them in `OFL.txt`, so opening an app
makes no request outside the box.

`material-symbols-rounded.woff2` above is the only font file in this
repository, and it is Apache-2.0.

No 3D avatar model ships with this repository. The 3D engines run only against
a model the household supplies (see
`dashboard/docs/AVATAR_SYSTEM.md`).

---

## Models are not distributed

No model weights are in this repository. The installer pulls them from Ollama
on the box at install time, so each one arrives under its own upstream licence
and is never redistributed by us:

| Model | Licence |
|---|---|
| `gpt-oss:20b` | Apache-2.0 |
| `qwen3:30b-a3b` | Apache-2.0 |
| `qwen2.5vl:7b` | see upstream model card (the Qwen2.5-VL family is not uniformly licensed — the 7B card is the authority) |
| `nomic-embed-text` | see upstream model card |

Piper (text-to-speech) is a **runtime dependency invoked as a separate
program** — PuffPAI shells out to the `piper` binary and links no Piper code
into its own process. Piper and its phonemizer carry GPL-3.0 components; see
the upstream project for the exact terms of the build you install. Piper voice
models are downloaded on the box and are covered by their own voice cards.

Speech-to-text is likewise **not distributed here**. Both engines arrive only
with the optional `[voice]` extra (`setup.py:87-119`), and each downloads its
own model on the box:

| Component | Role | Licence |
|---|---|---|
| `faster-whisper` | Whisper inference wrapper; the default STT engine (`PUFF_STT_ENGINE=whisper`) | see upstream (`SYSTRAN/faster-whisper`) — not present in the pip-licenses table below, so not verified here |
| CTranslate2 | compiled CPU/CUDA runtime `faster-whisper` links | see upstream (`OpenNMT/CTranslate2`) |
| Whisper model weights (`base.en` and friends) | downloaded at first use, never redistributed by us | see the model card for the exact model you pull |
| `vosk` | alternative offline STT engine | Apache-2.0 (also listed in the Python table below) |
| Vosk model packs | downloaded separately via `PUFF_VOSK_MODEL` | per-model; see the Vosk model page |

Exact licence strings for the pip-installed packages are in the Python table
below, which is what `pip-licenses` reported. Where this file says "see
upstream" it means we have not verified the string ourselves and you should
read the project's own LICENSE rather than trust a summary here.

---

## Python dependencies (base runtime + voice/dev/desktop extras, transitive)

| Name                                     | Version     | License                                                        | URL                                                                                                                |
|------------------------------------------|-------------|----------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------|
| Flask                                    | 3.1.3       | BSD-3-Clause                                                   | https://github.com/pallets/flask/                                                                                  |
| Jinja2                                   | 3.1.6       | BSD License                                                    | https://github.com/pallets/jinja/                                                                                  |
| MarkupSafe                               | 3.0.3       | BSD-3-Clause                                                   | https://github.com/pallets/markupsafe/                                                                             |
| PyPika                                   | 0.51.1      | Apache Software License                                        | https://github.com/kayak/pypika                                                                                    |
| PyYAML                                   | 6.0.3       | MIT License                                                    | https://pyyaml.org/                                                                                                |
| Pygments                                 | 2.21.0      | BSD-2-Clause                                                   | https://pygments.org                                                                                               |
| Werkzeug                                 | 3.1.8       | BSD-3-Clause                                                   | https://github.com/pallets/werkzeug/                                                                               |
| aiohappyeyeballs                         | 2.7.1       | Python Software Foundation License                             | https://github.com/aio-libs/aiohappyeyeballs                                                                       |
| aiohttp                                  | 3.14.3      | Apache-2.0 AND MIT                                             | https://github.com/aio-libs/aiohttp                                                                                |
| aiosignal                                | 1.4.0       | Apache Software License                                        | https://github.com/aio-libs/aiosignal                                                                              |
| altgraph                                 | 0.17.5      | MIT License                                                    | https://altgraph.readthedocs.io                                                                                    |
| annotated-doc                            | 0.0.5       | MIT                                                            | https://github.com/fastapi/annotated-doc                                                                           |
| annotated-types                          | 0.8.0       | MIT                                                            | https://github.com/annotated-types/annotated-types                                                                 |
| anyio                                    | 4.15.0      | MIT                                                            | https://anyio.readthedocs.io/en/stable/versionhistory.html                                                         |
| ast_serialize                            | 0.9.0       | MIT                                                            | https://github.com/mypyc/ast_serialize                                                                             |
| attrs                                    | 26.1.0      | MIT                                                            | https://www.attrs.org/en/stable/changelog.html                                                                     |
| bcrypt                                   | 5.0.0       | Apache Software License                                        | https://github.com/pyca/bcrypt/                                                                                    |
| black                                    | 26.5.1      | MIT                                                            | https://github.com/psf/black                                                                                       |
| blinker                                  | 1.9.0       | MIT License                                                    | https://github.com/pallets-eco/blinker/                                                                            |
| build                                    | 1.6.0       | MIT                                                            | https://build.pypa.io                                                                                              |
| certifi                                  | 2026.7.22   | Mozilla Public License 2.0 (MPL 2.0)                           | https://github.com/certifi/python-certifi                                                                          |
| cffi                                     | 2.1.1       | MIT-0                                                          | https://cffi.readthedocs.io/en/latest/whatsnew.html                                                                |
| charset-normalizer                       | 3.5.1       | MIT                                                            | https://github.com/jawah/charset_normalizer/blob/master/CHANGELOG.md                                               |
| chromadb                                 | 1.5.9       | Apache Software License                                        | https://github.com/chroma-core/chroma                                                                              |
| click                                    | 8.5.0       | BSD-3-Clause                                                   | https://github.com/pallets/click/                                                                                  |
| colorama                                 | 0.4.6       | BSD License                                                    | https://github.com/tartley/colorama                                                                                |
| cryptography                             | 50.0.1      | Apache-2.0 OR BSD-3-Clause                                     | https://github.com/pyca/cryptography                                                                               |
| durationpy                               | 0.11        | MIT                                                            | https://github.com/icholy/durationpy                                                                               |
| filelock                                 | 3.32.5      | MIT                                                            | https://github.com/tox-dev/py-filelock                                                                             |
| flake8                                   | 7.3.0       | MIT License                                                    | https://github.com/pycqa/flake8                                                                                    |
| flatbuffers                              | 25.12.19    | Apache Software License                                        | https://google.github.io/flatbuffers/                                                                              |
| frozenlist                               | 1.8.0       | Apache-2.0                                                     | https://github.com/aio-libs/frozenlist                                                                             |
| fsspec                                   | 2026.7.0    | BSD-3-Clause                                                   | https://github.com/fsspec/filesystem_spec                                                                          |
| googleapis-common-protos                 | 1.75.2      | Apache Software License                                        | https://github.com/googleapis/google-cloud-python/tree/main/packages/googleapis-common-protos                      |
| grpcio                                   | 1.83.1      | Apache-2.0                                                     | https://grpc.io                                                                                                    |
| h11                                      | 0.16.0      | MIT License                                                    | https://github.com/python-hyper/h11                                                                                |
| hf-xet                                   | 1.6.0       | Apache-2.0                                                     | https://github.com/huggingface/xet-core                                                                            |
| httpcore                                 | 1.0.9       | BSD-3-Clause                                                   | https://www.encode.io/httpcore/                                                                                    |
| httptools                                | 0.8.0       | MIT                                                            | https://github.com/MagicStack/httptools                                                                            |
| httpx                                    | 0.28.1      | BSD License                                                    | https://github.com/encode/httpx                                                                                    |
| huggingface_hub                          | 1.29.0      | Apache Software License                                        | https://github.com/huggingface/huggingface_hub                                                                     |
| idna                                     | 3.19        | BSD-3-Clause                                                   | https://github.com/kjd/idna                                                                                        |
| importlib_resources                      | 7.1.0       | Apache-2.0                                                     | https://github.com/python/importlib_resources                                                                      |
| iniconfig                                | 2.3.0       | MIT                                                            | https://github.com/pytest-dev/iniconfig                                                                            |
| itsdangerous                             | 2.2.0       | BSD License                                                    | https://github.com/pallets/itsdangerous/                                                                           |
| jaraco.classes                           | 3.4.0       | MIT License                                                    | https://github.com/jaraco/jaraco.classes                                                                           |
| jaraco.context                           | 6.1.2       | MIT                                                            | https://github.com/jaraco/jaraco.context                                                                           |
| jaraco.functools                         | 4.6.0       | MIT                                                            | https://github.com/jaraco/jaraco.functools                                                                         |
| jsonschema                               | 4.26.0      | MIT                                                            | https://github.com/python-jsonschema/jsonschema                                                                    |
| jsonschema-specifications                | 2025.9.1    | MIT                                                            | https://github.com/python-jsonschema/jsonschema-specifications                                                     |
| keyring                                  | 25.7.0      | MIT                                                            | https://github.com/jaraco/keyring                                                                                  |
| kubernetes                               | 36.0.3      | Apache Software License                                        | https://github.com/kubernetes-client/python                                                                        |
| librt                                    | 0.15.0      | MIT                                                            | https://github.com/mypyc/librt                                                                                     |
| markdown-it-py                           | 4.2.0       | MIT License                                                    | https://github.com/executablebooks/markdown-it-py                                                                  |
| mccabe                                   | 0.7.0       | MIT License                                                    | https://github.com/pycqa/mccabe                                                                                    |
| mdurl                                    | 0.1.2       | MIT License                                                    | https://github.com/executablebooks/mdurl                                                                           |
| mmh3                                     | 5.3.0       | MIT License                                                    | https://pypi.org/project/mmh3/                                                                                     |
| more-itertools                           | 11.1.0      | MIT                                                            | https://github.com/more-itertools/more-itertools                                                                   |
| multidict                                | 6.7.1       | Apache License 2.0                                             | https://github.com/aio-libs/multidict                                                                              |
| mypy                                     | 2.3.1       | MIT                                                            | https://www.mypy-lang.org/                                                                                         |
| mypy_extensions                          | 1.1.0       | MIT                                                            | https://github.com/python/mypy_extensions                                                                          |
| numpy                                    | 2.5.2       | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0             | https://numpy.org                                                                                                  |
| oauthlib                                 | 3.3.1       | BSD-3-Clause                                                   | https://github.com/oauthlib/oauthlib                                                                               |
| onnxruntime                              | 1.29.0      | MIT License                                                    | https://onnxruntime.ai                                                                                             |
| opentelemetry-api                        | 1.44.0      | Apache-2.0                                                     | https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-api                                 |
| opentelemetry-exporter-otlp-proto-common | 1.44.0      | Apache-2.0                                                     | https://github.com/open-telemetry/opentelemetry-python/tree/main/exporter/opentelemetry-exporter-otlp-proto-common |
| opentelemetry-exporter-otlp-proto-grpc   | 1.44.0      | Apache-2.0                                                     | https://github.com/open-telemetry/opentelemetry-python/tree/main/exporter/opentelemetry-exporter-otlp-proto-grpc   |
| opentelemetry-proto                      | 1.44.0      | Apache-2.0                                                     | https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-proto                               |
| opentelemetry-sdk                        | 1.44.0      | Apache-2.0                                                     | https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-sdk                                 |
| opentelemetry-semantic-conventions       | 0.65b0      | Apache-2.0                                                     | https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-semantic-conventions                |
| orjson                                   | 3.12.0      | MPL-2.0 AND (Apache-2.0 OR MIT)                                | https://github.com/ijl/orjson                                                                                      |
| overrides                                | 7.7.0       | Apache License, Version 2.0                                    | https://github.com/mkorpela/overrides                                                                              |
| packaging                                | 26.3        | Apache-2.0 OR BSD-2-Clause                                     | https://github.com/pypa/packaging                                                                                  |
| pathspec                                 | 1.1.1       | Mozilla Public License 2.0 (MPL 2.0)                           | https://python-path-specification.readthedocs.io/en/latest/index.html                                              |
| pathvalidate                             | 3.3.1       | MIT License                                                    | https://github.com/thombashi/pathvalidate                                                                          |
| pdfminer.six                             | 20260107    | MIT                                                            | https://github.com/pdfminer/pdfminer.six                                                                           |
| pdfplumber                               | 0.11.10     | MIT License                                                    | https://github.com/jsvine/pdfplumber                                                                               |
| pefile                                   | 2024.8.26   | MIT                                                            | https://github.com/erocarrera/pefile                                                                               |
| pillow                                   | 12.3.0      | MIT-CMU                                                        | https://python-pillow.github.io                                                                                    |
| piper-tts                                | 1.7.0       | GPL-3.0-or-later                                               | http://github.com/OHF-voice/piper1-gpl                                                                             |
| platformdirs                             | 4.11.7      | MIT                                                            | https://github.com/tox-dev/platformdirs                                                                            |
| pluggy                                   | 1.6.0       | MIT License                                                    | UNKNOWN                                                                                                            |
| propcache                                | 0.5.2       | Apache Software License                                        | https://github.com/aio-libs/propcache                                                                              |
| protobuf                                 | 7.36.1      | 3-Clause BSD License                                           | https://developers.google.com/protocol-buffers/                                                                    |
| pybase64                                 | 1.5.0       | BSD-2-Clause                                                   | https://github.com/mayeut/pybase64                                                                                 |
| pycodestyle                              | 2.14.0      | MIT                                                            | https://pycodestyle.pycqa.org/                                                                                     |
| pycparser                                | 3.0         | BSD-3-Clause                                                   | https://github.com/eliben/pycparser                                                                                |
| pydantic                                 | 2.13.5      | MIT                                                            | https://github.com/pydantic/pydantic                                                                               |
| pydantic-settings                        | 2.15.0      | MIT                                                            | https://github.com/pydantic/pydantic-settings                                                                      |
| pydantic_core                            | 2.46.5      | MIT                                                            | https://github.com/pydantic                                                                                        |
| pyflakes                                 | 3.4.0       | MIT License                                                    | https://github.com/PyCQA/pyflakes                                                                                  |
| pyinstaller                              | 6.22.2      | GNU General Public License v2 (GPLv2)                          | https://pyinstaller.org                                                                                            |
| pyinstaller-hooks-contrib                | 2026.7      | Apache Software License; GNU General Public License v2 (GPLv2) | https://github.com/pyinstaller/pyinstaller-hooks-contrib                                                           |
| pypdf                                    | 6.16.2      | BSD-3-Clause                                                   | https://github.com/py-pdf/pypdf                                                                                    |
| pypdfium2                                | 5.13.0      | BSD-3-Clause, Apache-2.0, dependency licenses                  | https://github.com/pypdfium2-team/pypdfium2                                                                        |
| pyproject_hooks                          | 1.2.0       | MIT License                                                    | https://github.com/pypa/pyproject-hooks                                                                            |
| pytest                                   | 9.1.1       | MIT                                                            | https://docs.pytest.org/en/latest/                                                                                 |
| python-dateutil                          | 2.9.0.post0 | Apache Software License; BSD License                           | https://github.com/dateutil/dateutil                                                                               |
| python-dotenv                            | 1.2.3       | BSD-3-Clause                                                   | https://github.com/theskumar/python-dotenv                                                                         |
| pytokens                                 | 0.4.1       | MIT License                                                    | https://github.com/tusharsadhwani/pytokens                                                                         |
| pywin32-ctypes                           | 0.2.3       | BSD-3-Clause                                                   | https://github.com/enthought/pywin32-ctypes                                                                        |
| referencing                              | 0.37.0      | MIT                                                            | https://github.com/python-jsonschema/referencing                                                                   |
| requests                                 | 2.34.2      | Apache Software License                                        | https://github.com/psf/requests                                                                                    |
| requests-oauthlib                        | 2.0.0       | BSD License                                                    | https://github.com/requests/requests-oauthlib                                                                      |
| rich                                     | 15.0.0      | MIT License                                                    | https://github.com/Textualize/rich                                                                                 |
| rpds-py                                  | 2026.6.3    | MIT                                                            | https://github.com/crate-py/rpds                                                                                   |
| shellingham                              | 1.5.4       | ISC License (ISCL)                                             | https://github.com/sarugaku/shellingham                                                                            |
| six                                      | 1.17.0      | MIT License                                                    | https://github.com/benjaminp/six                                                                                   |
| sounddevice                              | 0.5.6       | MIT                                                            | https://github.com/spatialaudio/python-sounddevice/                                                                |
| srt                                      | 3.5.3       | MIT License                                                    | https://github.com/cdown/srt                                                                                       |
| tenacity                                 | 9.1.4       | Apache Software License                                        | https://github.com/jd/tenacity                                                                                     |
| tokenizers                               | 0.23.1      | Apache Software License                                        | https://github.com/huggingface/tokenizers                                                                          |
| tqdm                                     | 4.70.0      | MPL-2.0 AND MIT                                                | https://tqdm.github.io                                                                                             |
| typer                                    | 0.27.2      | MIT                                                            | https://github.com/fastapi/typer                                                                                   |
| typing-inspection                        | 0.4.4       | MIT                                                            | https://github.com/pydantic/typing-inspection                                                                      |
| typing_extensions                        | 4.16.0      | PSF-2.0                                                        | https://github.com/python/typing_extensions                                                                        |
| urllib3                                  | 2.7.0       | MIT                                                            | https://github.com/urllib3/urllib3/blob/main/CHANGES.rst                                                           |
| uvicorn                                  | 0.52.4      | BSD-3-Clause                                                   | https://uvicorn.dev/                                                                                               |
| vosk                                     | 0.3.45      | Apache Software License                                        | https://github.com/alphacep/vosk-api                                                                               |
| watchfiles                               | 1.2.0       | MIT License                                                    | https://github.com/samuelcolvin/watchfiles                                                                         |
| websocket-client                         | 1.9.2       | Apache-2.0                                                     | https://github.com/websocket-client/websocket-client                                                               |
| websockets                               | 17.1        | BSD-3-Clause                                                   | https://github.com/python-websockets/websockets                                                                    |
| yarl                                     | 1.24.5      | Apache-2.0                                                     | https://github.com/aio-libs/yarl                                                                                   |
| zstandard                                | 0.25.0      | BSD-3-Clause                                                   | https://github.com/indygreg/python-zstandard                                                                       |

---

## JavaScript dependencies (dashboard/, direct only)

| Name | Version | License | Homepage |
|---|---|---|---|
| @dnd-kit/core | 6.3.1 | MIT | https://github.com/clauderic/dnd-kit#readme |
| @dnd-kit/sortable | 10.0.0 | MIT | https://github.com/clauderic/dnd-kit#readme |
| @dnd-kit/utilities | 3.2.2 | MIT | https://github.com/clauderic/dnd-kit#readme |
| @mdi/js | 7.4.47 | Apache-2.0 | https://github.com/Templarian/MaterialDesign-JS#readme |
| @react-three/drei | 10.7.8 | MIT | https://github.com/pmndrs/drei |
| @react-three/fiber | 9.7.0 | MIT | https://github.com/pmndrs/react-three-fiber#readme |
| @reduxjs/toolkit | 2.12.0 | MIT | https://redux-toolkit.js.org |
| @types/three | 0.185.4 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/three |
| chart.js | 4.5.1 | MIT | https://www.chartjs.org |
| formik | 2.4.9 | Apache-2.0 | https://formik.org |
| next | 16.3.4 | MIT | https://nextjs.org |
| numeral | 2.0.6 | MIT | http://numeraljs.com |
| react | 19.2.8 | MIT | https://react.dev/ |
| react-chartjs-2 | 5.3.1 | MIT | https://github.com/reactchartjs/react-chartjs-2 |
| react-dom | 19.2.8 | MIT | https://react.dev/ |
| react-redux | 9.3.0 | MIT | https://github.com/reduxjs/react-redux |
| swr | 2.5.1 | MIT | https://swr.vercel.app |
| three | 0.185.1 | MIT | https://threejs.org/ |
| @eslint/eslintrc | 3.3.7 | MIT | https://github.com/eslint/eslintrc#readme |
| @tailwindcss/forms | 0.5.11 | MIT | https://github.com/tailwindlabs/tailwindcss-forms#readme |
| @tailwindcss/postcss | 4.3.3 | MIT | https://tailwindcss.com |
| @types/lodash | 4.17.25 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/lodash |
| @types/node | 26.4.1 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/node |
| @types/numeral | 2.0.5 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/numeral |
| @types/react | 19.2.18 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/react |
| @types/react-dom | 19.2.5 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/react-dom |
| @types/react-redux | 7.1.34 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped/tree/master/types/react-redux |
| eslint | 10.9.1 | MIT | https://eslint.org |
| eslint-config-next | 16.3.4 | MIT | https://nextjs.org/docs/app/api-reference/config/eslint |
| eslint-config-prettier | 10.1.8 | MIT | https://github.com/prettier/eslint-config-prettier#readme |
| postcss | 8.5.26 | MIT | https://postcss.org/ |
| prettier | 3.9.6 | MIT | https://prettier.io |
| prettier-plugin-tailwindcss | 0.8.1 | MIT | https://github.com/tailwindlabs/prettier-plugin-tailwindcss#readme |
| tailwindcss | 4.3.3 | MIT | https://tailwindcss.com |
| typescript | 7.0.2 | Apache-2.0 | https://www.typescriptlang.org/ |
| talkinghead | github:met4citizen/TalkingHead#c22aed4 (2026-02-26) | MIT (per repo LICENSE) | https://github.com/met4citizen/TalkingHead |

---

## Notes

- **Python table** is the full transitive closure actually resolved by
  pip in a scratch virtualenv against the version constraints in
  `requirements.txt` and `setup.py`'s `voice`/`dev`/`desktop` extras
  — this is what a real install pulls in, not just the direct pins.
- **JavaScript table** lists only the packages named directly in
  `dashboard/package.json` (dependencies + devDependencies), with
  version/license/homepage resolved against each package's current
  `latest` npm registry entry rather than the exact semver-range pin —
  `node_modules` was not present in this worktree and installing it to
  run `npx license-checker` was not done in this pass (network-fetch of
  ~900 transitive packages). A full transitive JS notice (matching what
  `npm install` actually resolves, via `npx license-checker --summary`)
  is a follow-up; flagged in the Improve report.
- `talkinghead` is pulled directly from GitHub, pinned to commit
  `c22aed43249f9f8cbadf9cddda5b21b48f4732b3` (2026-02-26) rather than
  floating on the default branch; license taken from that repository's
  own LICENSE file (MIT), not the npm registry.
- The **Upstream template** section covers borrowed source code, which the
  package-manager tables cannot see: neither `pip-licenses` nor the npm
  registry knows that a component was copied from a template rather than
  installed from a registry.
- This file does not alter the project's own copyright/license
  (see `LICENSE`); it lists what the project *depends on*.

---

## Remaining direct dependencies

Every name in `requirements.txt` and `dashboard/package.json` that does not
appear anywhere above. Licences were read from installed package metadata on a
maintainer's machine (`pip show`, and each npm package's own `package.json`),
version noted so the claim is checkable. Full licence texts are not reproduced
here; each project ships its own, and the link is the authority.

Where a package was **not installed** when this table was compiled, it says so
rather than guessing. A licence asserted from memory in a notices file is worse
than an admitted gap, because it looks sourced. Installing the extras and
re-running `pip show` closes those rows.

### Python (`requirements.txt`)

| Package | Licence | Sourced from |
|---|---|---|
| `beautifulsoup4` | MIT | `pip show` (4.14.3) |
| `ddgs` | **not sourced** — see upstream | not installed here |
| `langchain` | **not sourced** — see upstream | not installed here |
| `langchain-community` | **not sourced** — see upstream | not installed here |
| `langchain-ollama` | MIT | `pip show` (1.0.1) |
| `langgraph` | MIT | package metadata `License-Expression` (1.0.10) |
| `openpyxl` | MIT | `pip show` (3.1.5) |
| `pdf2image` | **not sourced** — see upstream | not installed here |
| `pillow-heif` | **not sourced** — see upstream | not installed here |
| `pytesseract` | Apache-2.0 | `pip show` (0.3.13) |
| `python-docx` | MIT | `pip show` (1.2.0) |
| `python-pptx` | **not sourced** — see upstream | not installed here |

`pytesseract` is a wrapper: the Tesseract binary it drives (Apache-2.0) is a
system package you install yourself and is not redistributed here. `pdf2image`
is the same arrangement around Poppler.

### npm (`dashboard/package.json`)

| Package | Licence | Sourced from |
|---|---|---|
| `@types/qrcode` | MIT | its own `package.json` (1.5.5) |
| `qrcode` | MIT | its own `package.json` (1.5.4) |
| `react-grid-layout` | MIT | its own `package.json` (2.2.4) |
