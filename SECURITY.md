# Security

- AI provider keys are stored only in `~/.config/ai-cad/config.json` (permissions 600). They are never written to project files, logs or HTTP responses, and are dropped when you switch provider.
- The backend listens on `127.0.0.1` only. It has no authentication, so other programs on the same computer can reach it; do not expose its port to a network.
- The AI never executes code: it calls validated CAD commands. When the in-app chat uses Claude Code, that process may only use the AI CAD tools and read files you attach (shell, file editing and web tools are disallowed).

Report vulnerabilities privately to sainiheman.94@gmail.com rather than in a public issue.
