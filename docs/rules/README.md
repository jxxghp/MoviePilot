# Documentation Hub

This repository maintains a structured documentation library covering the full development lifecycle. All rule documents live in the `docs/rules/` directory. This index maps each file to its technical domain and intended reader.

---

## Technical Document Index

### Section I: Foundation and Environment

* **01 Project Overview**
  * File: `01-project-overview.md`
  * Scope: System goals, business domain, deployment models, and what is and is not in this repository.

* **02 Tech Stack**
  * File: `02-tech-stack.md`
  * Scope: Frameworks, languages, libraries, runtime environments, and third-party integrations.

* **03 Commands**
  * File: `03-commands.md`
  * Scope: CLI reference, development triggers, testing commands, linting, and dependency management.

### Section II: Architecture and Logic

* **04 Design Patterns**
  * File: `04-design-patterns.md`
  * Scope: Project-specific structural, creational, and behavioral patterns: Module, Chain, Event, Oper, Config Reload, Singleton.

* **05 Architecture and Modules**
  * File: `05-architecture.md`
  * Scope: Layer boundaries, dependency directions, module categories, and the canonical call graph.

* **09 External APIs, Protocols, and Responses**
  * File: `09-external-response.md`
  * Scope: HTTP client conventions, MCP protocol, standardized response formats, and error handling by layer.

* **10 Data and Persistent Management**
  * File: `10-data-and-persistent.md`
  * Scope: SQLAlchemy models, Alembic migrations, Oper access layer, SystemConfig, caching patterns.

### Section III: Implementation Standards

* **06 Code Standards and Style**
  * File: `06-code-styles.md`
  * Scope: Type annotations, Pydantic usage, async patterns, imports, formatting, and error handling rules.

* **07 Naming Conventions**
  * File: `07-naming-conventions.md`
  * Scope: Strict taxonomy for files, classes, functions, constants, and schema models.

* **08 Comments and Documentation Style**
  * File: `08-comment-styles.md`
  * Scope: Chinese docstring requirements, inline comment rules, and prohibited comment anti-patterns.

### Section IV: Quality and Governance

* **11 Code Quality and Security**
  * File: `11-quality-and-security.md`
  * Scope: Testing requirements, pylint gates, safety scans, authentication patterns, and input validation rules.

* **12 Collaboration, Versioning, Build, and Release**
  * File: `12-collaboration-and-distribution.md`
  * Scope: Conventional Commits, branch policy, release workflow, Docker build, and version management.

---

## Reader Persona Guidance

### Core Developers and Implementers

Developers actively writing or modifying features should follow this reading path:

1. **07 Naming Conventions** — establishes the lexicon for the feature.
2. **06 Code Standards** — ensures linting and logic compliance.
3. **04 Design Patterns** — identifies the correct structural approach.
4. **03 Commands** — required for local execution and validation.

### System Architects and Reviewers

Personnel focused on system integrity and long-term maintenance:

1. **05 Architecture and Modules** — for verifying structural boundaries.
2. **10 Data and Persistent Management** — for auditing data integrity and storage efficiency.
3. **09 External APIs** — for reviewing integration security and protocol compliance.
4. **11 Code Quality and Security** — for establishing the PR approval baseline.

### Operations and Release Engineers

Those managing the application lifecycle post-development:

1. **12 Collaboration and Versioning** — for release tags and branch management.
2. **02 Tech Stack** — for environment provisioning and dependency management.
3. **11 Code Quality and Security** — for verifying deployment-ready security posture.

---

## Document Interconnectivity

* **Architecture (05)** references **Code Standards (06)** for layer isolation and module boundary rules.
* **Naming Conventions (07)** works in tandem with **Comment Styles (08)** to define overall code readability.
* **External APIs (09)** relies on **Tech Stack (02)** for transport layer specifications and HTTP client selection.
* **Data Management (10)** is governed by **Quality and Security (11)** for sensitive data handling requirements.
* **Design Patterns (04)** is the implementation reference for decisions documented in **Architecture (05)**.

---

*Last Updated: 2026-05-25*
