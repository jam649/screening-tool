# Contributing

Read [the design](docs/design-v1.md) and [agent guidance](AGENTS.md). Keep changes focused, add regression coverage for behavioral changes, and run the checks described in the README.

Every commit requires a Developer Certificate of Origin (DCO) sign-off. By adding `Signed-off-by: Your Name <your@email.example>` with `git commit -s`, you certify the [Developer Certificate of Origin 1.1](https://developercertificate.org/). Use the identity you are entitled to contribute under. Automated CI verifies every commit in a pull request.

Use synthetic party data in issues and tests. Never upload customer books, passwords, API keys, OAuth tokens, databases, or session cookies. Report a suspected security issue privately through GitHub's security reporting tools where available.

Changes to matching behavior must preserve the published fixture bucket expectations; document any deliberate contract change and its rationale. Human dispositions and model outputs must always retain their original provenance.
