# Contributing to Mission Control

Thank you for your interest in contributing to Mission Control! This document provides guidelines and instructions for contributing.

## Code of Conduct

By participating in this project, you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

## How to Contribute

### Reporting Bugs

1. Check the [existing issues](../../issues) to avoid duplicates.
2. Open a new issue using the **Bug Report** template.
3. Include steps to reproduce, expected behavior, and actual behavior.
4. Add relevant logs, screenshots, or error messages.

### Suggesting Features

1. Open a new issue using the **Feature Request** template.
2. Describe the use case and why the feature would be valuable.
3. Be as specific as possible about the desired behavior.

### Submitting Pull Requests

1. **Fork** the repository and create a feature branch from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```
2. **Install** development dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev]"
   ```
3. **Make your changes** — keep commits focused and atomic.
4. **Run linting and tests** before submitting:
   ```bash
   ruff check agents/ tests/
   pytest
   ```
5. **Push** your branch and open a Pull Request against `main`.
6. Fill out the PR template completely.

### Coding Standards

- **Python 3.11+** is required.
- **Formatting**: We use `ruff` for linting (line length: 100).
- **Type hints**: Use type annotations for all function signatures.
- **Docstrings**: Add docstrings to public modules, classes, and functions.
- **Tests**: Add tests for new functionality in the `tests/` directory.
- **Commits**: Use clear, descriptive commit messages. Prefer [Conventional Commits](https://www.conventionalcommits.org/) format:
  ```
  feat: add new health check for disk usage
  fix: resolve stale task detection edge case
  docs: update agent configuration guide
  ```

### Project Structure

```
agents/                  # Main application code
├── mission_control/     # Core modules (database, MCP, scheduler)
└── squad/               # Agent implementations (jarvis, vision, etc.)
docs/                    # Documentation
infra/                   # Docker, systemd, deployment
tests/                   # Test suite
```

### Development Setup

See the [README](README.md#quick-start) for full setup instructions.

**Quick start:**
```bash
cp .env.example .env
# Edit .env with your configuration
pip install -e ".[dev]"
python -m agents.cli init-db
python -m agents.cli seed-agents
```

## Questions?

Feel free to open an issue for any questions about contributing. We're happy to help!
