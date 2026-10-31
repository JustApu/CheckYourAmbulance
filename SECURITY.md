# Security policy

Check Your Ambulance is pre-release software under active development. It is not supported for production use, and only the current `main` branch receives fixes.

## Reporting a vulnerability

Report vulnerabilities privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability**. Do not open a public issue, pull request or discussion about a vulnerability.

Include the affected file, route or command; the steps to reproduce; and the impact you expect. Use synthetic values only. Never include real credentials, personal data, patient or medical information, operational logs or production configuration, even if you found them elsewhere.

Reports are reviewed by the maintainer as time allows. There is no bug bounty and no guaranteed response time.

## Scope

In scope: the application code, database migrations and triggers, deployment documentation and the CI workflows in this repository.

Out of scope: third-party dependencies (report those upstream), the `prototype/` and `spike/` directories, which are not deployed, and findings that need an already-compromised server or administrator account.
