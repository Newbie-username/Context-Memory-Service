# AGENTS.md

## Git 提交与推送

每轮任务完成并验证后，只提交本轮修改，直接推送到 `origin` 的 `main` 分支，并返回 GitHub 提交链接。

## 1. Execution Style & Autonomy

- **Bias toward action**: When requirements are clear, edit files, refactor code, and run verification commands directly. Do NOT pause to ask for confirmation on routine edits, file creation, or local dependency installation.
- **Decisive refactoring**: If existing code blocks a clean implementation, refactor or delete it directly instead of working around it with hacks.
- **No backward-compatibility shims**: Do not keep deprecated functions, unused exports, or legacy fallbacks unless explicitly requested.
- **Fail fast**: Only stop and ask me if you hit a genuine architectural ambiguity or a failing test/build you cannot resolve after 2 attempts.
- **Concise reporting**: Skip lengthy preambles and step-by-step narration. Report what changed, what was verified, and any remaining blockers.

## 2. Anti-Bloat & Clean Code

- **No defensive bloat**: Do not add redundant null/undefined checks, speculative fallbacks, or broad `try/catch` blocks around code that is already type-safe or validated at the boundary.
- **Validate at boundaries only**: Trust internal function signatures and types. Only validate external inputs (HTTP payloads, env vars, third-party APIs).
- **Avoid premature abstraction**: Do not create wrapper classes, factory patterns, or helper files for one-off logic. Prefer direct, readable code.
- **Minimal comments**: Do not add comments that restate what the code does. Only comment non-obvious business rules or workarounds.

## 3. Stack-Specific Rules

### Python (FastAPI / Backend / Scripts)

- Use modern Python 3.11+ type hints (`list[str]`, `str | None`).
- Let Pydantic / FastAPI handle request validation; do not duplicate validation checks inside route handlers.
- Never use bare `except:` or swallow exceptions silently. Let unexpected errors surface to the global error handler.
- Prefer plain functions and dataclasses/Pydantic models over unnecessary class hierarchies.

### Go

- Follow idiomatic Go error handling: return wrapped errors (`fmt.Errorf("...: %w", err)`). Do not both log and return the same error.
- Define interfaces on the consumer side only when multiple implementations or mocking actually require them.
- Avoid `init()` magic and unnecessary pointer indirection for small structs.

### React / Next.js (TypeScript)

- Trust TypeScript types; do not add runtime `if (!prop)` guards for required typed props.
- Default to Server Components in Next.js App Router; add `"use client"` only when hooks, browser APIs, or event handlers are required.
- Do not use `useEffect` to sync derived state—compute it during render.
- Do not wrap everything in `useMemo` / `useCallback` by default; only optimize measured bottlenecks or unstable effect dependencies.

## 4. Verification

- After edits, run the narrowest relevant check first (e.g., `pytest <file>`, `go test ./...`, `npm run typecheck` / `npm run lint`).
- Fix any type, lint, or test failures directly before handing back control.
