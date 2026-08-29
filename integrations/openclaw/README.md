# Optional OpenClaw integration

OpenClaw remains a separately installed external dependency. Jarvis Home communicates through a bounded adapter and does not vendor the OpenClaw runtime or state.

Before enabling the integration, configure a dedicated least-privilege agent, keep high-risk actions behind confirmation, and never commit the OpenClaw workspace, credentials, session history or gateway state.

If OpenClaw source or binaries are ever included in a distribution, their license and third-party notices must be shipped with that distribution.
