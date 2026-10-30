# Releasing

Releases go to PyPI as `callables`. Pushing a version tag runs `.github/workflows/publish.yml`, which tests, builds and publishes through PyPI trusted publishing (no stored token).

1. Bump `__version__` in `callables/__init__.py` and commit.
2. `git tag v0.1.1 && git push origin main v0.1.1`
3. To make deployed functions use it, rebuild the worker image with `bin/build-platform.sh --push` in callables-api, which takes the SDK from `../callables-python`. Builds copy whatever SDK version the image has installed.

Existing images keep the SDK they were built with, and the API reads the result and log markers in `callables/runtime.py`. If those markers, the `CALLABLES_*` environment variables or `python -m callables run` change, every project has to be redeployed.

## One-time PyPI setup

1. On https://pypi.org, under **Your account → Publishing → Add a new pending publisher**: project name `callables`, owner `callables-app`, repository `callables-python`, workflow `publish.yml`, environment `pypi`.
2. In this GitHub repo, **Settings → Environments → New environment** named `pypi` (optionally with yourself as a required reviewer).
3. The first tag pushed after that creates the project on PyPI. Until step 1 is done the publish job fails, so nothing can be published by accident.
