# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

If you discover a security vulnerability in Mission Control, please report it responsibly.

**Do not open a public issue.**

Instead, please email **ambus1989@gmail.com** with:

1. A description of the vulnerability
2. Steps to reproduce the issue
3. The potential impact
4. Any suggested fixes (optional)

### What to Expect

- **Acknowledgment**: Within 48 hours of your report.
- **Assessment**: We will investigate and determine the severity within 7 days.
- **Resolution**: We aim to release a fix within 30 days for critical issues.
- **Credit**: We will credit reporters in the release notes (unless you prefer anonymity).

## Security Best Practices

When deploying Mission Control:

- **Never commit `.env` files** — use `.env.example` as a template.
- **Rotate API tokens** regularly (GitHub, Telegram, DigitalOcean, etc.).
- **Use least-privilege tokens** — grant only the permissions each integration needs.
- **Keep dependencies updated** — Dependabot is configured to alert on vulnerable packages.
- **Run behind a reverse proxy** in production (e.g., nginx with TLS).
- **Restrict database access** — use strong passwords and network-level access controls.

## Scope

This policy covers the Mission Control codebase and its official deployments. Third-party integrations (GitHub, Telegram, DigitalOcean) are governed by their own security policies.
