# mobile-broom tasks

# Run tests
test:
    uv run pytest -q

# Format + lint
fmt:
    uvx ruff format src tests
    uvx ruff check --fix src tests

# Run the tool from source
run *ARGS:
    uv run mobile-broom {{ARGS}}

# Format + test
check: fmt test

# Tag and push a release (reads version from pyproject.toml)
release: check
    #!/usr/bin/env bash
    version=$(uv run python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
    echo "Releasing v${version}"
    git tag "v${version}"
    git push && git push --tags
    # mise's pipx backend resolves versions from GitHub RELEASES, not bare tags
    gh release create "v${version}" --title "v${version}" --generate-notes
