# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| 1.2.x | ✅ |
| < 1.2 | ❌ |

## Reporting a vulnerability

Please **do not open a public issue** for security-sensitive reports.

Use GitHub's private vulnerability reporting on this repository:

1. Go to the **Security** tab.
2. Choose **Report a vulnerability**.
3. Include the affected version, reproduction steps, impact, and any suggested fix.

You can expect an acknowledgement within a few days. Please give a reasonable window for a fix before public disclosure.

## Security model

Cutout Studio is designed as a **local, single-user application**:

- The server binds to `127.0.0.1` only.
- The MediaKit API key is stored in the local `.env` file; the browser receives only a masked value.
- `POST /api/*` requests with an `Origin` header must come from `127.0.0.1`, `localhost` or `::1`.
- `/api/cutout` additionally requires the `X-Cutout-Client: 1` header.
- Result file IDs must match a 32-character lowercase hex pattern before any file is read.
- User-controlled filenames are sanitized (path separators, control characters, Unicode bidi controls, Windows reserved names, length).
- Image decoding is guarded by a 60-megapixel limit; result downloads are capped at 80 MB.
- The application has no telemetry and makes no third-party requests beyond the MediaKit calls initiated by the user.

### What is explicitly out of scope

The following are **not** protected against, and should be addressed before any deployment beyond localhost:

- Multi-user access, account isolation, or per-user quotas.
- Public internet exposure, TLS termination, reverse-proxy configuration.
- Malicious local users or a compromised operating system.
- Audit logging, rate limiting at the application layer, or enterprise IAM.
- Data-at-rest encryption for `outputs/` and `.env`.

If you deploy behind a reverse proxy or on a LAN, add authentication, TLS, request rate limiting and network-level access controls, and re-evaluate the CSRF assumptions above.

## Secret handling

- Never commit `.env`; it is excluded by `.gitignore`.
- `.env.example` contains an empty key and is safe to commit.
- If a key is exposed, revoke it in the [AI MediaKit console](https://console.volcengine.com/imp/ai-mediakit/settings) and create a new one. Removing it from the latest commit is not enough if it reached Git history or a fork.
- Do not paste real keys, signed result URLs, or personal photos into issues or pull requests. Use synthetic or redacted material.
- Screenshots in this repository use generated demo images and a fake masked key.

## Dependency hygiene

Runtime dependencies are listed in `requirements.txt`; development-only dependencies in `requirements-dev.txt`. CI installs from these files and runs the test suite on every push and pull request. For local checks:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Consider running `pip-audit` in your own environment if you deploy modified versions.
