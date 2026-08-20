# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Setup
```bash
uv sync                                    # Install dependencies
uv run python backend/ops/scripts/seed_synthetic.py   # Create database + demo accounts
```

### Running the Application
```bash
uv run backend/app/main.py                 # Start the development server
# Access at http://127.0.0.1:8000
```

### Testing
```bash
uv run pytest -q                           # Run all tests (48 tests)
uv run pytest tests/test_auth.py -v        # Run authentication tests with verbose output
uv run pytest tests/test_web.py -v         # Run web interface tests
```

### Linting and Formatting
```bash
uv run ruff check backend tests            # Lint the codebase
uv run ruff format backend tests           # Format the codebase
```

### Database Management
```bash
rm curanode.db                             # Reset the database (delete SQLite file)
# Then re-seed with: uv run python backend/ops/scripts/seed_synthetic.py
```

## Code Architecture

### High-Level Structure
```
backend/
  app/                         # Main FastAPI application
    main.py                    # ASGI app entrypoint with startup guards and middleware
    settings.py                # Application configuration with fail-fast validation
    paths.py                   # Directory location constants
    deps.py                    # Authentication dependencies and role-based access control
    errors.py                  # Error handling taxonomy and envelope
    cache.py                   # TTL store for sessions, lockouts, rate limits
    log_config.py              # Structured logging with PII redaction
    db/                        # Database layer (models, async session, UUIDv7)
    identity/                  # Authentication system (router, service, schemas, security)
    audit/                     # Append-only audit writer
    i18n/                      # English/Urdu message catalogues
    web/                       # Server-rendered page routes and form handling
frontend/
  templates/                   # Jinja2 templates (base, auth pages, shell, partials)
  static/                      # CSS (design tokens + app) and minimal JS
tests/                         # Test suite (authentication and web interface)
docs/                          # Product requirements, technical design, design system
.claude/specs/                 # Feature specifications and implementation plans
```

### Key Architectural Decisions

1. **Authentication & Session Management**
   - JWT access tokens (15 min) + rotating refresh tokens (14 days) stored in HttpOnly cookies
   - Refresh tokens are single-use; replaying spent tokens revokes the entire token family
   - Role verification happens per-request from database (not cached in tokens) for immediate revocation
   - Four explicit dependencies in `deps.py`: `ActorDep`, `PatientDep`, `VerifiedDoctorDep`, `ClinicAdminDep`
   - No ad-hoc role checks anywhere in the codebase

2. **Security Properties**
   - Argon2id password hashing (time_cost=3, memory_cost=65536, parallelism=4)
   - Fail-indistinguishable authentication errors (same timing, same response for wrong password/unknown email/suspended account)
   - Registration is not an oracle (duplicate emails return success-shaped response but create nothing)
   - Account lockout after 10 consecutive failures (15-minute lockout)
   - Startup guards prevent running with weak JWT secret or non-synthetic data in non-pilot environments

3. **Internationalization**
   - English and Urdu support with right-to-left layout
   - Locale as URL prefix (`/en/` or `/ur/`) allows language switching without losing place
   - Message catalogues in JSON format with Jinja2 template integration

4. **Technology Stack**
   - **API & Pages**: FastAPI with server-rendered Jinja2 templates (chosen over SPA for single deployable)
   - **Persistence**: SQLAlchemy 2.0 (async) over SQLite (dev/test) with aiosqlite
   - **Validation**: Pydantic 2.x with email validation
   - **Security**: Python-JWT for tokens, Argon2-CFFI for password hashing
   - **Observability**: Structlog for structured logging
   - **Development**: Uvicorn with reload, Ruff for linting/formatting, Pytest for testing

5. **Project-Specific Constraints**
   - Designed to run on student laptops (uses SQLite + in-process cache instead of PostgreSQL + Redis)
   - All development/test data is synthetic by requirement
   - No Alembic migrations yet (schema created at startup for SQLite)
   - Server-rendered templates replace Next.js frontend from original TDD

### Important Files to Understand First

1. **`backend/app/main.py`** - Application entrypoint, middleware, startup guards
2. **`backend/app/deps.py`** - Core authentication and role-based access control system
3. **`backend/app/identity/router.py`** - JSON API authentication endpoints
4. **`backend/app/web/router.py`** - Server-rendered page handlers and form processing
5. **`backend/app/settings.py`** - Configuration with validation and fail-fast checks
6. **`tests/conftest.py`** - Test setup showing how to override dependencies for isolated testing

### When Making Changes

- **Authentication changes**: Focus on `deps.py`, `identity/`, and related tests
- **UI changes**: Work with Jinja2 templates in `frontend/templates/` and `web/router.py`
- **Database changes**: Modify `db/models.py` and understand the async session patterns
- **Configuration**: Update `settings.py` with appropriate validation guards
- **Testing**: Follow existing patterns in `tests/` using the fixtures in `conftest.py`

The application prioritizes security and correctness over convenience, with deliberate choices like per-request role verification and fail-indistinguishable error responses to prevent enumeration attacks.